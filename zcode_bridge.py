"""Local JSON transport to the optional ZCode native runtime bridge."""
import asyncio
import json
import os
import subprocess


class BridgeFailure(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def child_env():
    return {"HOME": os.environ.get("HOME", "/tmp"), "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


def decode_output(output, returncode):
    decoder = json.JSONDecoder()
    result, offset = None, 0
    while (start := output.find("{", offset)) >= 0:
        try:
            value, offset = decoder.raw_decode(output, start)
        except ValueError:
            offset = start + 1
            continue
        if isinstance(value, dict) and value.get("pdfTranslateZcodeBridge") == 1:
            result = value
    if result is None:
        raise BridgeFailure("ZCode 运行桥返回格式无效")
    if result.get("error"):
        raise BridgeFailure(str(result["error"]))
    if returncode:
        raise BridgeFailure("ZCode 运行桥执行失败")
    return result


def run_sync(executable, payload, timeout=30):
    try:
        proc = subprocess.run([executable], input=json.dumps(payload), capture_output=True, text=True, env=child_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BridgeFailure("ZCode 模型列表读取超时", 504)
    except OSError:
        raise BridgeFailure("无法启动 ZCode 运行桥，请检查本机配置")
    return decode_output(proc.stdout, proc.returncode)


async def run_async(executable, payload, timeout):
    try:
        proc = await asyncio.create_subprocess_exec(executable, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env())
    except OSError:
        raise BridgeFailure("无法启动 ZCode 运行桥，请检查本机配置")
    communication = asyncio.create_task(proc.communicate(json.dumps(payload).encode()))
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(communication), 3)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await communication
        if isinstance(error, asyncio.CancelledError):
            raise
        raise BridgeFailure("ZCode 回答超时", 504)
    return decode_output(stdout.decode(errors="replace"), proc.returncode)
