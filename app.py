# app.py
from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
import socketio
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from datetime import datetime
import uvicorn
import json

# Database setup
SQLALCHEMY_DATABASE_URL = "sqlite:///./chat_app.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Database Models
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    socket_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    messages = relationship("Message", back_populates="user")

class Channel(Base):
    __tablename__ = "channels"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    messages = relationship("Message", back_populates="channel")

class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text)
    user_id = Column(Integer, ForeignKey("users.id"))
    channel_id = Column(Integer, ForeignKey("channels.id"))
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="messages")
    channel = relationship("Channel", back_populates="messages")

# Create tables
Base.metadata.create_all(bind=engine)

# Dependency to get database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Initialize FastAPI app
app = FastAPI(title="Discord-like Chat App")

# Socket.IO setup
sio = socketio.AsyncServer(cors_allowed_origins="*", async_mode='asgi')
socket_app = socketio.ASGIApp(sio, app)

# Templates setup
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Store active users and their channels
active_users = {}
user_channels = {}

# Helper functions
def create_default_channels():
    db = SessionLocal()
    try:
        # Check if channels exist
        if db.query(Channel).count() == 0:
            default_channels = [
                Channel(name="general", description="General discussion"),
                Channel(name="random", description="Random conversations"),
                Channel(name="tech", description="Technology discussions"),
            ]
            for channel in default_channels:
                db.add(channel)
            db.commit()
    finally:
        db.close()

def get_user_by_socket_id(socket_id: str, db: Session):
    return db.query(User).filter(User.socket_id == socket_id).first()

def get_channel_messages(channel_id: int, db: Session, limit: int = 50):
    return db.query(Message).filter(Message.channel_id == channel_id)\
             .order_by(Message.timestamp.desc()).limit(limit).all()[::-1]

# FastAPI Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, username: str):
    db = SessionLocal()
    try:
        channels = db.query(Channel).all()
        return templates.TemplateResponse("chat.html", {
            "request": request,
            "username": username,
            "channels": channels
        })
    finally:
        db.close()

@app.get("/api/channels")
async def get_channels(db: Session = Depends(get_db)):
    channels = db.query(Channel).all()
    return [{"id": channel.id, "name": channel.name, "description": channel.description} 
            for channel in channels]

@app.get("/api/channels/{channel_id}/messages")
async def get_channel_messages_api(channel_id: int, db: Session = Depends(get_db)):
    messages = get_channel_messages(channel_id, db)
    return [{
        "id": msg.id,
        "content": msg.content,
        "username": msg.user.username,
        "timestamp": msg.timestamp.isoformat(),
        "channel_id": msg.channel_id
    } for msg in messages]

# Socket.IO Events
@sio.event
async def connect(sid, environ):
    print(f"Client {sid} connected")

@sio.event
async def disconnect(sid):
    print(f"Client {sid} disconnected")
    db = SessionLocal()
    try:
        user = get_user_by_socket_id(sid, db)
        if user:
            # Update user status
            user.socket_id = None
            db.commit()
            
            # Remove from active users
            if sid in active_users:
                username = active_users[sid]
                del active_users[sid]
                
                # Notify all channels the user left
                if sid in user_channels:
                    for channel_id in user_channels[sid]:
                        await sio.emit('user_left', {
                            'username': username,
                            'channel_id': channel_id
                        }, room=f"channel_{channel_id}")
                    del user_channels[sid]
    finally:
        db.close()

@sio.event
async def join_app(sid, data):
    username = data['username']
    db = SessionLocal()
    try:
        # Check if user exists, create if not
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(username=username, socket_id=sid)
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            user.socket_id = sid
            db.commit()
        
        active_users[sid] = username
        user_channels[sid] = set()
        
        await sio.emit('joined_app', {
            'username': username,
            'user_id': user.id
        }, room=sid)
        
    finally:
        db.close()

@sio.event
async def join_channel(sid, data):
    channel_id = data['channel_id']
    db = SessionLocal()
    try:
        user = get_user_by_socket_id(sid, db)
        channel = db.query(Channel).filter(Channel.id == channel_id).first()
        
        if user and channel:
            # Join the socket.io room for this channel
            await sio.enter_room(sid, f"channel_{channel_id}")
            
            # Track user's channels
            if sid in user_channels:
                user_channels[sid].add(channel_id)
            
            # Get recent messages for this channel
            messages = get_channel_messages(channel_id, db)
            message_data = [{
                "id": msg.id,
                "content": msg.content,
                "username": msg.user.username,
                "timestamp": msg.timestamp.isoformat(),
                "channel_id": msg.channel_id
            } for msg in messages]
            
            await sio.emit('channel_joined', {
                'channel_id': channel_id,
                'channel_name': channel.name,
                'messages': message_data
            }, room=sid)
            
            # Notify others in the channel
            await sio.emit('user_joined', {
                'username': user.username,
                'channel_id': channel_id
            }, room=f"channel_{channel_id}", skip_sid=sid)
            
    finally:
        db.close()

@sio.event
async def leave_channel(sid, data):
    channel_id = data['channel_id']
    db = SessionLocal()
    try:
        user = get_user_by_socket_id(sid, db)
        
        if user:
            # Leave the socket.io room
            await sio.leave_room(sid, f"channel_{channel_id}")
            
            # Remove from user's channels
            if sid in user_channels:
                user_channels[sid].discard(channel_id)
            
            # Notify others in the channel
            await sio.emit('user_left', {
                'username': user.username,
                'channel_id': channel_id
            }, room=f"channel_{channel_id}")
            
    finally:
        db.close()

@sio.event
async def send_message(sid, data):
    channel_id = data['channel_id']
    content = data['content']
    
    db = SessionLocal()
    try:
        user = get_user_by_socket_id(sid, db)
        channel = db.query(Channel).filter(Channel.id == channel_id).first()
        
        if user and channel and content.strip():
            # Save message to database
            message = Message(
                content=content,
                user_id=user.id,
                channel_id=channel_id
            )
            db.add(message)
            db.commit()
            db.refresh(message)
            
            # Broadcast message to all users in the channel
            message_data = {
                'id': message.id,
                'content': message.content,
                'username': user.username,
                'timestamp': message.timestamp.isoformat(),
                'channel_id': channel_id
            }
            
            await sio.emit('new_message', message_data, room=f"channel_{channel_id}")
            
    finally:
        db.close()

@sio.event
async def create_channel(sid, data):
    channel_name = data['name']
    channel_description = data.get('description', '')
    
    db = SessionLocal()
    try:
        user = get_user_by_socket_id(sid, db)
        
        if user:
            # Check if channel already exists
            existing_channel = db.query(Channel).filter(Channel.name == channel_name).first()
            if existing_channel:
                await sio.emit('error', {
                    'message': f'Channel "{channel_name}" already exists'
                }, room=sid)
                return
            
            # Create new channel
            new_channel = Channel(
                name=channel_name,
                description=channel_description
            )
            db.add(new_channel)
            db.commit()
            db.refresh(new_channel)
            
            # Broadcast new channel to all connected users
            channel_data = {
                'id': new_channel.id,
                'name': new_channel.name,
                'description': new_channel.description
            }
            
            await sio.emit('channel_created', channel_data)
            
    finally:
        db.close()

# Initialize default channels
create_default_channels()

if __name__ == "__main__":
    uvicorn.run("app:socket_app", host="0.0.0.0", port=8000, reload=True)