import os
from pathlib import Path
import json
import time
import random
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="MathQuiz - Minimal Live Exam")

# ==================== Core In-Memory State ====================
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
        self.is_live = True
        self.live_duration_seconds = duration_minutes * 60
        self.start_timestamp = time.time()
        self.submissions = []
        
        # สุ่มลำดับข้อสอบ 1 ลำดับ เพื่อให้นักเรียนทุกคนเห็นโจทย์เรียงเหมือนกัน
        shuffled = list(self.raw_quiz["questions"])
        random.shuffle(shuffled)
        self.live_shuffled_questions = shuffled
        return True

    def stop_live(self):
        self.is_live = False
        self.start_timestamp = 0.0

    def get_remaining_seconds(self) -> int:
        if not self.is_live:
            return 0
        elapsed = time.time() - self.start_timestamp
        remaining = int(self.live_duration_seconds - elapsed)
        if remaining <= 0:
            self.stop_live()
            return 0
        return remaining

    def add_submission(self, student_name: str, answers: Dict[str, int]) -> Dict[str, Any]:
        if not self.raw_quiz:
            return {"score": 0, "total": 0, "details": {}}
        
        questions = self.raw_quiz["questions"]
        q_map = {str(q["id"]): q for q in questions}
        
        score = 0
        details = {}
        for q_id_str, chosen_idx in answers.items():
            if q_id_str in q_map:
                correct_idx = q_map[q_id_str]["correct_answer_index"]
                is_correct = (chosen_idx == correct_idx)
                if is_correct:
                    score += 1
                details[q_id_str] = {
                    "chosen": chosen_idx,
                    "correct": correct_idx,
                    "is_correct": is_correct,
                    "explanation": q_map[q_id_str].get("explanation", "")
                }
        
        self.submissions.append({
            "student_name": student_name,
            "score": score,
            "total": len(questions),
            "answers": details,
            "submitted_at": time.time()
        })
        
        return {"score": score, "total": len(questions), "details": details}

    def get_analytics(self) -> Dict[str, Any]:
        if not self.raw_quiz or not self.submissions:
            total_q = len(self.raw_quiz["questions"]) if self.raw_quiz else 0
            return {
                "total_students": len(self.submissions),
                "histogram": {"labels": [f"{i}" for i in range(total_q + 1)], "data": [0]*(total_q + 1)},
                "error_ranking": []
            }
        
        total_q = len(self.raw_quiz["questions"])
        scores = [sub["score"] for sub in self.submissions]
        
        # 1. Histogram ความถี่คะแนน
        hist_data = [0] * (total_q + 1)
        for s in scores:
            if 0 <= s <= total_q:
                hist_data[s] += 1
                
        # 2. วิเคราะห์ข้อที่ตอบผิด (Item Analysis)
        q_stats = {}
        for q in self.raw_quiz["questions"]:
            q_id = str(q["id"])
            q_stats[q_id] = {
                "id": q["id"],
                "question": q["question"],
                "choices": q["choices"],
                "correct_answer_index": q["correct_answer_index"],
                "wrong_count": 0,
                "total_answered": 0,
                "choice_counts": [0, 0, 0, 0]
            }

        for sub in self.submissions:
            for q_id_str, ans in sub["answers"].items():
                if q_id_str in q_stats:
                    q_stats[q_id_str]["total_answered"] += 1
                    chosen = ans["chosen"]
                    if 0 <= chosen < 4:
                        q_stats[q_id_str]["choice_counts"][chosen] += 1
                    if not ans["is_correct"]:
                        q_stats[q_id_str]["wrong_count"] += 1

        error_ranking = []
        for q_id, data in q_stats.items():
            err_rate = (data["wrong_count"] / data["total_answered"] * 100) if data["total_answered"] > 0 else 0
            error_ranking.append({
                "id": data["id"],
                "question": data["question"],
                "choices": data["choices"],
                "correct_answer_index": data["correct_answer_index"],
                "wrong_count": data["wrong_count"],
                "total_answered": data["total_answered"],
                "error_rate": round(err_rate, 1),
                "choice_counts": data["choice_counts"]
            })

        error_ranking.sort(key=lambda x: x["error_rate"], reverse=True)

        return {
            "total_students": len(self.submissions),
            "histogram": {"labels": [f"{i} คะแนน" for i in range(total_q + 1)], "data": hist_data},
            "error_ranking": error_ranking
        }

state = LiveQuizState()

# ==================== Page Routes ====================
@app.get("/", response_class=HTMLResponse)
async def student_page(request: Request):
    return templates.TemplateResponse(request=request, name="student.html")

@app.get("/teacher", response_class=HTMLResponse)
async def teacher_page(request: Request):
    return templates.TemplateResponse(request=request, name="teacher.html")

# ==================== API Endpoints ====================
@app.get("/api/template")
async def get_template():
    template_data = {
        "quiz_title": "ใส่ชื่อชุดข้อสอบ",
        "time_limit_minutes": 15,
        "questions": [
            {
                "id": 1,
                "question": "จงคำนวณค่าของ $\\sqrt{144} + 5^2$",
                "choices": ["$37$", "$39$", "$24$", "$17$"],
                "correct_answer_index": 0,
                "explanation": "เพราะ $\\sqrt{144} = 12$ และ $5^2 = 25$ ดังนั้น $12 + 25 = 37$"
            }
        ]
    }
    return JSONResponse(content=template_data, headers={"Content-Disposition": "attachment; filename=quiz_template.json"})

@app.post("/api/upload")
async def upload_quiz(file: UploadFile = File(...)):
    try:
        content = await file.read()
        quiz_data = json.loads(content.decode("utf-8"))
        if "questions" not in quiz_data or len(quiz_data["questions"]) == 0:
            return JSONResponse(status_code=400, content={"status": "error", "message": "ไฟล์ JSON ไม่มีคำถาม"})
        state.set_quiz(quiz_data)
        await notify_teachers({"type": "QUIZ_UPDATED", "quiz": quiz_data})
        return {"status": "success", "title": quiz_data.get("quiz_title"), "count": len(quiz_data["questions"])}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

@app.get("/api/quiz-status")
async def quiz_status():
    has_quiz = (state.raw_quiz is not None) and (len(state.raw_quiz.get("questions", [])) > 0)
    return {
        "has_quiz": has_quiz,
        "is_live": state.is_live,
        "remaining_seconds": state.get_remaining_seconds(),
        "quiz_title": state.raw_quiz.get("quiz_title", "") if has_quiz else "",
        "total_questions": len(state.raw_quiz.get("questions", [])) if has_quiz else 0,
        "time_limit_minutes": state.raw_quiz.get("time_limit_minutes", 10) if has_quiz else 10
    }

@app.get("/api/get-quiz")
async def get_quiz():
    if not state.is_live or not state.live_shuffled_questions:
        return JSONResponse(status_code=400, content={"message": "ยังไม่เปิดให้ทำข้อสอบ"})
    
    # ส่งข้อสอบแบบซ่อนเฉลย (Security by Design)
    clean_questions = [
        {"id": q["id"], "question": q["question"], "choices": q["choices"]}
        for q in state.live_shuffled_questions
    ]
    return {
        "quiz_title": state.raw_quiz["quiz_title"],
        "time_limit": state.get_remaining_seconds(),
        "questions": clean_questions
    }

@app.post("/api/start-timer")
async def start_timer(request: Request):
    data = await request.json()
    duration = int(data.get("duration", 10))
    if state.start_live(duration):
        await notify_teachers({"type": "SESSION_STARTED", "duration": duration * 60})
        return {"status": "success"}
    return JSONResponse(status_code=400, content={"status": "error", "message": "ไม่สามารถเริ่มสอบได้ (ยังไม่ได้อัปโหลดข้อสอบ)"})

@app.post("/api/stop-timer")
async def stop_timer():
    state.stop_live()
    await notify_teachers({"type": "SESSION_STOPPED"})
    return {"status": "success"}

@app.post("/api/submit")
async def submit_quiz(request: Request):
    data = await request.json()
    name = data.get("student_name", "Anonymous")
    answers = data.get("answers", {})
    result = state.add_submission(name, answers)
    
    # ส่งสัญญาณอัปเดตสถิติให้อาจารย์ทันที
    analytics = state.get_analytics()
    await notify_teachers({"type": "NEW_SUBMISSION", "analytics": analytics})
    return {"status": "success", **result}

# ==================== Real-time WebSocket ====================
@app.websocket("/ws/teacher")
async def ws_teacher_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.connected_teachers.append(websocket)
    try:
        init_data = {
            "type": "INIT",
            "is_live": state.is_live,
            "remaining_seconds": state.get_remaining_seconds(),
            "quiz": state.raw_quiz,
            "analytics": state.get_analytics()
        }
        await websocket.send_text(json.dumps(init_data))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        state.connected_teachers.remove(websocket)

async def notify_teachers(message: dict):
    text_data = json.dumps(message)
    for ws in list(state.connected_teachers):
        try:
            await ws.send_text(text_data)
        except Exception:
            state.connected_teachers.remove(ws)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)