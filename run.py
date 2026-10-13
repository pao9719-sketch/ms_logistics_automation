"""개발/현업 PC용 실행: python run.py  →  http://127.0.0.1:5000"""
import os

from mslog.web import create_app

app = create_app()

if __name__ == "__main__":
    # 기본은 로컬 접속만 허용. 사내망 공유 시 MSLOG_HOST=0.0.0.0 (접근통제 별도 필요)
    app.run(host=os.environ.get("MSLOG_HOST", "127.0.0.1"), port=int(os.environ.get("MSLOG_PORT", "5000")))
