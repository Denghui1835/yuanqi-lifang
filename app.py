# -*- coding: utf-8 -*-
"""元气立方 · 阶段零本机演示服务。

跑起来：  python app.py       然后打开 http://127.0.0.1:8775
"""
import io
import sys

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import xiaoyuan as xy   # 它已经处理过 stdout 编码，这里不要再包一层

app = FastAPI(title='元气立方 · 阶段零演示')


class Msg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: list[Msg]


@app.get('/api/daily')
def api_daily():
    return xy.daily()


@app.get('/api/kb')
def api_kb():
    """给前端渲染「调理手册」页用。"""
    return {
        'constitutions': xy.CONSTITUTIONS,
        'patterns': xy.PATTERNS,
        'baduanjin': xy.KB['baduanjin'],
        'baduanjin_rx': xy.KB['baduanjin_rx'],
        'classics': xy.KB['classics'],
        'solar_terms': xy.KB['solar_terms'],
        'scripts': xy.KB['scripts'],
    }


@app.post('/api/chat')
def api_chat(body: ChatIn):
    hist = [m.model_dump() for m in body.messages]
    try:
        return xy.reply(hist)
    except Exception as e:
        return JSONResponse(status_code=200, content={
            'reply': '（小元这边连接出了点问题，稍后再试试）',
            'crisis': None, 'cards': [],
            'diagnosis': {'patterns': [], 'constitutions': []},
            'error': '%s: %s' % (type(e).__name__, e),
        })


app.mount('/static', StaticFiles(directory='static'), name='static')


@app.get('/')
def index():
    return FileResponse('static/index.html')


if __name__ == '__main__':
    import uvicorn
    print('元气立方 · 阶段零演示  ->  http://127.0.0.1:8775')
    uvicorn.run(app, host='127.0.0.1', port=8775, log_level='warning')
