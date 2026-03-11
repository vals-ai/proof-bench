#!/usr/bin/env python3
"""Shared loogle daemon - single subprocess serving multiple workers via HTTP.

Usage:
    python -m proof_bench.loogle_daemon --port 8765

Workers query via HTTP. Requests serialize through asyncio.Lock.
"""

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "lean-lsp-mcp" / "loogle"


class LoogleProcess:
    """Wraps loogle binary with serialized stdin/stdout communication."""

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> str | None:
        """Start loogle subprocess. Returns error message or None on success."""
        return await self._start_subprocess()

    async def _start_subprocess(self) -> str | None:
        """Internal: start or restart the loogle subprocess."""
        binary = CACHE_DIR / "repo" / ".lake" / "build" / "bin" / "loogle"
        if not binary.exists():
            return f"Binary not found: {binary}"

        indices = list((CACHE_DIR / "index").glob("*.idx"))
        if not indices:
            return f"No index in {CACHE_DIR / 'index'}"

        self.proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--json",
            "--interactive",
            "--read-index",
            str(indices[0]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CACHE_DIR / "repo",
            limit=1024 * 1024,
        )

        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=120)
            if b"Loogle is ready" in line:
                print("[INFO] Loogle subprocess started")
                return None
            return f"Unexpected startup: {line.decode()[:100]}"
        except TimeoutError:
            return "Startup timeout"

    async def query(self, q: str, limit: int = 8) -> dict:
        """Send query to loogle and return parsed response."""
        q = q.replace("\n", " ").replace("\r", " ").strip()
        if not q:
            return {"error": "Empty query"}

        async with self.lock:
            if not self.proc or self.proc.returncode is not None:
                if self.proc and self.proc.returncode is not None:
                    print(f"[WARN] Loogle process died (code {self.proc.returncode}), restarting...")
                else:
                    print("[WARN] Loogle process not running, starting...")

                err = await self._start_subprocess()
                if err:
                    return {"error": f"Failed to restart: {err}"}

            try:
                query_bytes = f"{q}\n".encode()
                print(f"[DEBUG] Query: {repr(q)[:100]}")
                self.proc.stdin.write(query_bytes)
                await self.proc.stdin.drain()

                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
                line_str = line.decode().strip()

                if not line_str:
                    print("[WARN] Empty response from loogle")
                    try:
                        stderr = await asyncio.wait_for(self.proc.stderr.read(1000), timeout=0.5)
                        if stderr:
                            print(f"[DEBUG] stderr: {stderr.decode()[:200]}")
                    except TimeoutError:
                        pass
                    return {"error": "Empty response from loogle"}

                data = json.loads(line_str)
                print(f"[DEBUG] Got response with {len(data.get('hits', []))} hits")

            except TimeoutError:
                return {"error": "Query timeout"}
            except json.JSONDecodeError as e:
                print(f"[ERROR] JSON decode failed: {e}, raw: {line_str[:200]}")
                return {"error": f"Invalid response: {e}"}
            except (BrokenPipeError, ConnectionResetError, ConnectionError) as e:
                print(f"[ERROR] Connection to loogle lost: {e}")
                self.proc = None
                return {"error": f"Loogle subprocess died: {e}"}
            except Exception as e:
                print(f"[ERROR] Query failed: {e}")
                return {"error": str(e)}

            if err := data.get("error"):
                return {"error": err}

            return {
                "items": [
                    {
                        "name": h.get("name", ""),
                        "type": h.get("type", ""),
                        "module": h.get("module", ""),
                    }
                    for h in data.get("hits", [])[:limit]
                ]
            }

    async def stop(self):
        """Terminate the subprocess."""
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except TimeoutError:
                self.proc.kill()


class Server:
    """Minimal HTTP server for loogle queries."""

    def __init__(self, loogle: LoogleProcess, host: str, port: int):
        self.loogle = loogle
        self.host = host
        self.port = port
        self.server: asyncio.Server | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming HTTP request."""
        try:
            header_data = b""
            while b"\r\n\r\n" not in header_data:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
                if not chunk:
                    break
                header_data += chunk

            if b"\r\n\r\n" not in header_data:
                await self._respond(writer, 400, {"error": "Invalid request"})
                return

            header_part, body_start = header_data.split(b"\r\n\r\n", 1)
            request = header_part.decode()

            content_length = 0
            for line in request.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
                    break

            body = body_start
            while len(body) < content_length:
                chunk = await asyncio.wait_for(reader.read(content_length - len(body)), timeout=5)
                if not chunk:
                    break
                body += chunk
            body_str = body.decode()

            if request.startswith("GET /health"):
                await self._respond(writer, 200, {"status": "ok"})
            elif request.startswith("POST /query"):
                try:
                    params = json.loads(body_str) if body_str.strip() else {}
                except json.JSONDecodeError as e:
                    print(f"[ERROR] JSON parse failed: {e}, body={body_str[:200]!r}")
                    params = {}
                result = await self.loogle.query(
                    params.get("query", ""),
                    params.get("max_results", 8),
                )
                await self._respond(writer, 200, result)
            else:
                await self._respond(writer, 404, {"error": "Not found"})
        except Exception as e:
            await self._respond(writer, 500, {"error": str(e)})
        finally:
            writer.close()
            await writer.wait_closed()

    async def _respond(self, writer: asyncio.StreamWriter, status: int, body: dict):
        """Send HTTP response."""
        payload = json.dumps(body).encode()
        status_text = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "Error")

        writer.write(f"HTTP/1.1 {status} {status_text}\r\n".encode())
        writer.write(b"Content-Type: application/json\r\n")
        writer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
        writer.write(payload)
        await writer.drain()

    async def run(self):
        """Start server and wait for shutdown signal."""
        self.server = await asyncio.start_server(self.handle, self.host, self.port)
        print(f"Listening on {self.host}:{self.port}")

        loop = asyncio.get_event_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        async with self.server:
            await stop.wait()

        print("\nShutting down...")
        await self.loogle.stop()


async def main(host: str, port: int):
    loogle = LoogleProcess()
    print("Starting loogle...")

    if err := await loogle.start():
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    print("Loogle ready")
    await Server(loogle, host, port).run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shared loogle HTTP daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    asyncio.run(main(args.host, args.port))
