import asyncio
import os
import re
import json
import time
import random
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from hashlib import sha3_256

import toml
from pydantic import BaseModel
from snowflake import SnowflakeGenerator

from fastapi import FastAPI, Request, Response
from starlette.middleware.sessions import SessionMiddleware

from utils import generate_novelai_image, free_check, set_token


id_gen = SnowflakeGenerator(1)


class GenerateRequest(BaseModel):
    prompt: str
    neg_prompt: str
    seed: int
    scale: float
    width: int
    height: int
    steps: int
    sampler: str
    schedule: str
    smea: bool = False
    dyn: bool = False
    dyn_threshold: bool = False
    cfg_rescale: float = 0.0
    img_sub_folder: str = ''
    extra_infos: str = ''


server_config = toml.load("config.toml")["gen_server"]
auth_configs = server_config.get('auth', [])
set_token(server_config['token'])
prev_gen_time = time.time()

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=uuid4().hex)

generate_semaphore = asyncio.Semaphore(server_config['max_jobs'])
save_worker = ThreadPoolExecutor(16)


def save_img(save_path: str, sub_folder: str, image: bytes, json: str):
    if not save_path:
        save_path = server_config['save_path']
    sub_folder_path = os.path.join(save_path, sub_folder)
    os.makedirs(sub_folder_path, exist_ok=True)
    os.makedirs(f'{sub_folder_path}/metadatas', exist_ok=True)
    
    img_hash = sha3_256(image).hexdigest()
    img_id = next(id_gen)
    
    with open(os.path.join(sub_folder_path, f'{img_id}_{img_hash[:8]}.png'), 'wb') as f:
        f.write(image)
    with open(os.path.join(f'{sub_folder_path}/metadatas', f'{img_id}_{img_hash[:8]}.json'), 'w', encoding='utf-8') as f:
        f.write(json)


@app.post("/login")
async def login(password: str, request: Request):
    for auth in auth_configs:
        if password == auth['password']:
            request.session['signed'] = True
            request.session['free_only'] = auth.get('free_only', True)
            request.session['save_path'] = auth.get('save_path', server_config['save_path'])
            request.session['custom_sub_folder'] = auth.get('custom_sub_folder', False)
            return {"status": "login success"}
    else:
        request.session.clear()
        return Response(json.dumps({'status': 'login failed'}), 403)


@app.post("/gen")
async def gen(context: GenerateRequest, request: Request):
    global prev_gen_time
    
    signed = request.session.get('signed', False)
    freeonly = request.session.get('free_only', True)
    try:
        extra_infos = json.loads(context.extra_infos)
    except:
        return Response(json.dumps({'status': 'Extra infos in invalid format, please send json strings.'}), 403)
    always_require_auth = server_config['always_require_auth']
    is_free_gen = free_check(context.width, context.height, context.steps)
    
    save_path = request.session.get('save_path', server_config['save_path'])
    if request.session.get('custom_sub_folder', False):
        sub_folder = context.img_sub_folder or extra_infos.get('save_folder', '')
    else:
        sub_folder = ''
    safe_folder_name = re.sub(r'[^\w\-_\. ]', '_', sub_folder)
    
    if ((not signed and (always_require_auth or not is_free_gen))
        or (freeonly and not is_free_gen)):
        return Response(json.dumps({'status': 'Config not allowed'}), 403)
    
    async with generate_semaphore:
        if prev_gen_time + server_config['min_delay'] > time.time():
            await asyncio.sleep(server_config['min_delay'] + random.random()*0.3)
        prev_gen_time = time.time()
        
        img_bytes, json_payload = await generate_novelai_image(
            context.prompt,
            False,
            context.neg_prompt,
            "",
            context.seed,
            context.scale,
            context.width,
            context.height,
            context.steps,
            context.sampler,
            context.schedule,
            context.smea,
            context.dyn,
            context.dyn_threshold,
            context.cfg_rescale,
        )
    
    if not isinstance(img_bytes, bytes):
        error_mes = img_bytes
        response = json_payload
        try:
            error_response = response.json()
        except:
            error_response = response.text
        return Response(json.dumps({'error-mes': error_mes, 'status': error_response}), 500)
    
    await asyncio.get_running_loop().run_in_executor(
        save_worker, save_img, save_path, safe_folder_name, img_bytes, json_payload
    )
    
    return Response(img_bytes, media_type="image/png")


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=server_config['host'], port=server_config['port'])
    input('Press Enter to close...')