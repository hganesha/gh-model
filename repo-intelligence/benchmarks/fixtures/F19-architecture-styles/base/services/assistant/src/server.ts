import express from "express";
import rateLimit from "express-rate-limit";
import { answer } from "./agent";

const app = express();
app.use(express.json());
app.use(rateLimit({ windowMs: 60_000, limit: 30 }));

app.post("/ask", async (req, res) => {
  res.json({ answer: await answer(req.body.question) });
});

app.listen(3000);
