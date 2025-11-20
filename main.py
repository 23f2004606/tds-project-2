import os
import json
import asyncio
import requests
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from playwright.async_api import async_playwright
import google.generativeai as genai

# --- CONFIGURATION ---
load_dotenv()
app = FastAPI()

# Configure Gemini (Use 1.5 Flash for speed)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# The secret you put in the Google Form
MY_SECRET = os.getenv("MY_STUDENT_SECRET", "default_secret")

class QuizTask(BaseModel):
    email: str
    secret: str
    url: str

# --- HELPER FUNCTIONS ---

def generate_solver_code(question_context):
    """
    Asks Gemini to write Python code to solve the quiz question.
    """
    prompt = f"""
    You are an expert Python automation script writer. 
    Below is the text content of a quiz page. It contains a data question.
    
    TASK:
    1. Identify the data source (CSV url, API, or data in the text).
    2. Write a COMPLETE Python script to calculate the answer.
    3. Assign the final answer to a variable named 'final_answer'.
    
    CONTEXT:
    {question_context}
    
    RULES:
    - Use 'requests', 'pandas', or standard libraries.
    - Handle errors gracefully.
    - DO NOT print the answer, just assign it to 'final_answer'.
    - RETURN ONLY PYTHON CODE. No markdown blocks. No comments.
    """
    
    try:
        response = model.generate_content(prompt)
        code = response.text.replace("```python", "").replace("```", "").strip()
        return code
    except Exception as e:
        print(f"LLM Error: {e}")
        return ""

def extract_submission_details(text):
    """
    Asks Gemini to extract the submission URL and JSON format.
    """
    prompt = f"""
    Analyze this text and extract the URL where I need to POST the answer.
    Return ONLY the URL as a plain string.
    
    TEXT:
    {text}
    """
    response = model.generate_content(prompt)
    return response.text.strip()

# --- CORE LOGIC ---

async def process_quiz_cycle(task_url: str, email: str, secret: str):
    print(f"\n>>> STARTING TASK: {task_url}")
    
    # 1. Scrape the Page (Headless Browser)
    page_text = ""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(task_url, timeout=30000)
            # Wait for the JavaScript to decode the base64 content
            await page.wait_for_selector("body")
            # Wait a tiny bit extra for dynamic content
            await asyncio.sleep(2) 
            page_text = await page.inner_text("body")
        except Exception as e:
            print(f"Scraping Error: {e}")
            return
        await browser.close()

    print(f"--- Scraped Content ({len(page_text)} chars) ---")
    
    # 2. Ask Gemini for Code
    code = generate_solver_code(page_text)
    print("--- Generated Code ---")
    # print(code) # Uncomment to debug

    # 3. Execute Code
    local_scope = {}
    try:
        # DANGER: executing AI code. Necessary for this hackathon.
        exec(code, {}, local_scope)
        answer = local_scope.get("final_answer")
        print(f"--- Calculated Answer: {answer} ---")
    except Exception as e:
        print(f"Execution Error: {e}")
        traceback.print_exc()
        answer = None

    # 4. Determine Submission URL
    # Usually the prompt says "Post to https://..."
    submit_url = extract_submission_details(page_text)
    
    # Sanity check the URL
    if not submit_url.startswith("http"):
        # Fallback: assume it's the same domain or a standard endpoint
        print("Warning: Could not find submit URL. Using default guess.")
        submit_url = "https://example.com/submit" # Replace with logic if needed

    # 5. Submit Answer
    payload = {
        "email": email,
        "secret": secret,
        "url": task_url,
        "answer": answer
    }
    
    print(f"--- Submitting to {submit_url} ---")
    try:
        submit_response = requests.post(submit_url, json=payload, timeout=10)
        response_data = submit_response.json()
        print(f"Response: {response_data}")
        
        # 6. RECURSION: Check if there is a next quiz
        if response_data.get("correct") and response_data.get("url"):
            next_url = response_data["url"]
            print(f">>> RECURSING TO NEXT URL: {next_url}")
            # Recursive call
            await process_quiz_cycle(next_url, email, secret)
            
    except Exception as e:
        print(f"Submission Failed: {e}")

# --- API ENDPOINTS ---

@app.get("/")
def home():
    return {"status": "active", "message": "Send POST to /quiz"}

@app.post("/quiz")
async def handle_quiz(task: QuizTask, background_tasks: BackgroundTasks):
    # 1. Verify Secret
    if task.secret != MY_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    
    # 2. Schedule work in background (Non-blocking)
    background_tasks.add_task(process_quiz_cycle, task.url, task.email, task.secret)
    
    # 3. Respond immediately
    return {"message": "Task accepted. Processing in background."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)