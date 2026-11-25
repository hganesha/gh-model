import express from "express";
import { answer } from "./agent";

const app = express();
app.use(express.json());

app.post("/ask", async (req, res) => {
  res.json({ answer: await answer(req.body.question) });
});

app.listen(3000);
