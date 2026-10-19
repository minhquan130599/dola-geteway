from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path
import uuid
import os


app = FastAPI()

@app.post("/3d_captcha_tiktok/")
async def solve_3d_captcha(file: UploadFile = File(...)):
    """
    API endpoint to solve the 3D captcha.
    """
    from tiktok_captcha.tiktok_3d_captcha import solve_3d_captcha_tiktok
    file_id = uuid.uuid4()
    TEMP_DIR = Path("temp")
    TEMP_DIR.mkdir(exist_ok=True)
    file_path = TEMP_DIR / f"{file_id}_{file.filename}"
    try:
        with open(file_path, "wb") as f:
            f.write(await file.read())

        res = solve_3d_captcha_tiktok(str(file_path))
        if res:
            pointA, pointB = res
            return JSONResponse(content={"ok": True, "pointA": pointA, "pointB": pointB})
        return JSONResponse(content={"ok": False, "detail": "No matching pair found"}, status_code=200)

    except Exception as e:
        raise e #re-raise the exception, so the error handler can catch it.

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            

@app.post("/forum_captcha/")
async def solve_forum_captcha(file: UploadFile = File(...)):
    """
    API endpoint to solve the forum captcha.
    """
    from forum_captcha.predict import process_captcha
    file_id = uuid.uuid4()
    TEMP_DIR = Path("temp")
    TEMP_DIR.mkdir(exist_ok=True)
    file_path = TEMP_DIR / f"{file_id}_{file.filename}"
    try:
        with open(file_path, "wb") as f:
            f.write(await file.read())

        text = process_captcha(file_path)

        return JSONResponse(content={"text" : text})

    except Exception as e:
        raise e #re-raise the exception, so the error handler can catch it.

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=15000)
