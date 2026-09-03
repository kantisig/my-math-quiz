import os
import json
import time
import random
import asyncio
from pathlib import Path
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="MathQuiz - Minimal Live Exam")

class LiveQuizState:
    def __init__(self):
        self.raw_quiz: Optional[Dict[str, Any]] = None
        self.is_live: bool = False
        self.live_duration_seconds: int = 0
        self.start_timestamp: float = 0.0
        self.live_shuffled_questions: List[Dict[str, Any]] = []
        self.submissions: List[Dict[str, Any]] = []
        self.connected_teachers: List[WebSocket] = []

    def set_quiz(self, quiz_data: Dict[str, Any]):
        self.raw_quiz = quiz_data
        self.is_live = False
        self.submissions = []
        self.live_shuffled_questions = []

    def start_live(self, duration_minutes: int) -> bool:
        if not self.raw_quiz or not self.raw_quiz.get("questions"):
            return False
        shuffled = list(self.raw_quiz["questions"])
        random.shuffle(shuffled)
        self.live_shuffled_questions = shuffled
        self.live_duration_seconds = duration_minutes * 60
        self.start_timestamp = time.time()
        self.submissions = []
        self.is_live = True
        return True

    def stop_live(self):
        self.is_live = False

    def get_remaining_seconds(self) -> int:
        if not self.is_live: return 0
        elapsed = time.time() - self.start_timestamp
        remaining = int(self.live_duration_seconds - elapsed)
        return max(0, remaining)

    def check_status(self):
        if self.is_live and self.get_remaining_seconds() <= 0:
            self.stop_live()
        return self.is_live

    def add_submission(self, name: str, answers: Dict[str, int]):
        if not self.check_status() or not self.raw_quiz: return None
        questions = self.raw_quiz["questions"]
        q_map = {str(q["id"]): q for q in questions}
        score = 0
        details = {}
        for q_id, chosen in answers.items():
            if q_id in q_map:
                correct = q_map[q_id]["correct_answer_index"]
                is_ok = (chosen == correct)
                if is_ok: score += 1
                details[q_id] = {"chosen": chosen, "correct": correct, "is_correct": is_ok, "explanation": q_map[q_id].get("explanation", "")}
        
        entry = {"student_name": name, "score": score, "total": len(questions), "answers": details, "at": time.time()}
        self.submissions.append(entry)
        return entry

    def get_analytics(self):
        if not self.raw_quiz: return {}
        total_q = len(self.raw_quiz["questions"])
        hist = [0] * (total_q + 1)
        for s in self.submissions: hist[s["score"]] += 1
        
        q_stats = []
        for q in self.raw_quiz["questions"]:
            q_id = str(q["id"])
            wrong = sum(1 for s in self.submissions if q_id in s["answers"] and not s["answers"][q_id]["is_correct"])
            total_ans = sum(1 for s in self.submissions if q_id in s["answers"])
            q_stats.append({
                "question": q["question"],
                "wrong_count": wrong,
                "error_rate": round((wrong/total_ans*100),1) if total_ans > 0 else 0
            })
        q_stats.sort(key=lambda x: x["error_rate"], reverse=True)
        return {"total_students": len(self.submissions), "histogram": hist, "error_ranking": q_stats}

state = LiveQuizState()

async def notify_teachers(msg: dict):
    for ws in list(state.connected_teachers):
        try: await ws.send_json(msg)
        except: state.connected_teachers.remove(ws)

@app.get("/", response_class=HTMLResponse)
async def student_page(request: Request): return templates.TemplateResponse("student.html", {"request": request})

@app.get("/teacher", response_class=HTMLResponse)
async def teacher_page(request: Request): return templates.TemplateResponse("teacher.html", {"request": request})

@app.post("/api/upload")
async def upload_quiz(file: UploadFile = File(...)):
    data = json.loads((await file.read()).decode())
    state.set_quiz(data)
    await notify_teachers({"type": "QUIZ_UPDATED", "quiz": data})
    return {"status": "success"}

@app.get("/api/quiz-status")
async def quiz_status():
    state.check_status()
    return {"is_live": state.is_live, "remaining": state.get_remaining_seconds(), "title": state.raw_quiz.get("quiz_title", "") if state.raw_quiz else ""}

@app.get("/api/get-quiz")
async def get_quiz():
    if not state.check_status(): return JSONResponse(status_code=400, content={"msg": "Closed"})
    return {"title": state.raw_quiz["quiz_title"], "questions": [{"id": q["id"], "question": q["question"], "choices": q["choices"]} for q in state.live_shuffled_questions], "remaining": state.get_remaining_seconds()}

@app.post("/api/start-timer")
async def start_timer(request: Request):
    d = await request.json()
    if state.start_live(int(d.get("duration", 10))):
        await notify_teachers({"type": "SESSION_STARTED"})
        return {"status": "success"}

@app.post("/api/stop-timer")
async def stop_timer():
    state.stop_live()
    await notify_teachers({"type": "SESSION_STOPPED"})
    return {"status": "success"}

@app.post("/api/submit")
async def submit_quiz(request: Request):
    d = await request.json()
    res = state.add_submission(d.get("student_name"), d.get("answers", {}))
    if res:
        await notify_teachers({"type": "NEW_SUBMISSION", "analytics": state.get_analytics()})
        return res
    return JSONResponse(status_code=400, content={"msg": "Late"})

@app.websocket("/ws/teacher")
async def ws_teacher(websocket: WebSocket):
    await websocket.accept()
    state.connected_teachers.append(websocket)
    try:
        await websocket.send_json({"type": "INIT", "is_live": state.is_live, "analytics": state.get_analytics(), "quiz": state.raw_quiz})
        while True: await websocket.receive_text()
    except WebSocketDisconnect: state.connected_teachers.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)