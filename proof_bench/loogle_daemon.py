#!/usr/bin/env python3
"""Shared loogle daemon - single subprocess serving multiple workers via HTTP.

Usage:
    python -m proof_bench.loogle_daemon --port 8765

Workers query via HTTP. Requests serialize through asyncio.Lock.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "lean-lsp-mcp" / "loogle"
DEFAULT_BINARY = CACHE_DIR / "repo" / ".lake" / "build" / "bin" / "loogle"


class LoogleProcess:
    """Wraps loogle binary with serialized stdin/stdout communication."""

    def __init__(self, binary: Path | None = None, index: Path | None = None):
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self._binary = binary
        self._index = index

    async def start(self) -> str | None:
        """Start loogle subprocess. Returns error message or None on success."""
        return await self._start_subprocess()

    async def _start_subprocess(self) -> str | None:
        """Internal: start or restart the loogle subprocess."""
        binary = self._binary or DEFAULT_BINARY
        if not binary.exists():
            return f"Binary not found: {binary}"

        if self._index is not None:
            index = self._index
        else:
            indices = list((CACHE_DIR / "index").glob("*.idx"))
            if not indices:
                return f"No index in {CACHE_DIR / 'index'}"
            index = indices[0]

        self.proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--json",
            "--interactive",
            "--read-index",
            str(index),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )

        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=120)
            if b"Loogle is ready" in line:
                logger.info("Loogle subprocess started")
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
                    logger.warning("Loogle process died (code %d), restarting...", self.proc.returncode)
                else:
                    logger.warning("Loogle process not running, starting...")

                err = await self._start_subprocess()
                if err:
                    return {"error": f"Failed to restart: {err}"}

            try:
                query_bytes = f"{q}\n".encode()
                logger.debug("Query: %s", repr(q)[:100])
                self.proc.stdin.write(query_bytes)
                await self.proc.stdin.drain()

                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
                line_str = line.decode().strip()

                if not line_str:
                    logger.warning("Empty response from loogle")
                    try:
                        stderr = await asyncio.wait_for(self.proc.stderr.read(1000), timeout=0.5)
                        if stderr:
                            logger.debug("stderr: %s", stderr.decode()[:200])
                    except TimeoutError:
                        pass
                    return {"error": "Empty response from loogle"}

                data = json.loads(line_str)
                logger.debug("Got response with %d hits", len(data.get("hits", [])))

            except TimeoutError:
                return {"error": "Query timeout"}
            except json.JSONDecodeError as e:
                logger.error("JSON decode failed: %s, raw: %s", e, line_str[:200])
                return {"error": f"Invalid response: {e}"}
            except (BrokenPipeError, ConnectionResetError, ConnectionError) as e:
                logger.error("Connection to loogle lost: %s", e)
                self.proc = None
                return {"error": f"Loogle subprocess died: {e}"}
            except Exception as e:
                logger.error("Query failed: %s", e)
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
                    logger.error("JSON parse failed: %s, body=%r", e, body_str[:200])
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
        logger.info("Listening on %s:%d", self.host, self.port)

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        async with self.server:
            await stop.wait()

        logger.info("Shutting down")
        await self.loogle.stop()


async def main(host: str, port: int, binary: Path | None = None, index: Path | None = None):
    loogle = LoogleProcess(binary=binary, index=index)
    logger.info("Starting loogle...")

    if err := await loogle.start():
        logger.error("Failed to start: %s", err)
        sys.exit(1)

    logger.info("Loogle ready")
    await Server(loogle, host, port).run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser(description="Shared loogle HTTP daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--binary", type=Path, default=None)
    parser.add_argument("--index", type=Path, default=None)
    args = parser.parse_args()

    asyncio.run(main(args.host, args.port, binary=args.binary, index=args.index))
