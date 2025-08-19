import express , {Request,Response,NextFunction} from 'express';
import cors from 'cors';

const app = express();

const port = 3000;

app.use(cors());

app.use((req: Request, res: Response,next: NextFunction) => {
    console.log(`${req.method} ${req.url}`);
    next();
});

app.get('/',(req:Request , res: Response) => {
    res.send("welcome to homepage");
})

