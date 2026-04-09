#!/usr/bin/env python3
"""
Claude Cockpit — Claude Code 워크스페이스 모니터링 대시보드 백엔드
Python 3.9+ / 외부 의존성 없음 (stdlib only)
"""

import json
import mimetypes
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

# ─── 설정 ────────────────────────────────────────────────────────────────────
BIND_HOST = "0.0.0.0"
BIND_PORT = 8080
INTERNAL_IP = "192.168.57.17"

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
DIST_DIR = Path(__file__).parent / "dist"

SSE_INTERVAL = 5  # 초


# ─── 유틸리티 ────────────────────────────────────────────────────────────────

def read_json(path: Path) -> Optional[Any]:
    """JSON 파일을 안전하게 읽기. 실패 시 None 반환."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return None


def read_text(path: Path, max_chars: int = 0) -> Optional[str]:
    """텍스트 파일을 안전하게 읽기. max_chars > 0이면 잘라냄."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if max_chars > 0 and len(content) > max_chars:
            return content[:max_chars]
        return content
    except (FileNotFoundError, PermissionError, UnicodeDecodeError):
        return None


def parse_frontmatter(text: str) -> dict:
    """YAML frontmatter를 간이 파싱 (--- 블록). PyYAML 없이 key: value만 추출."""
    result = {}
    if not text.startswith("---"):
        return result
    end = text.find("---", 3)
    if end == -1:
        return result
    block = text[3:end].strip()
    for line in block.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip().strip('"').strip("'")
        # tools 같은 리스트 처리 (인라인 [a, b, c])
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
        result[key] = val
    return result


def decode_project_path(folder_name: str) -> str:
    """프로젝트 폴더명을 실제 경로로 변환. '-home-sungjoo-repo' → '/home/sungjoo/repo'

    Claude Code는 '/'를 '-'로 치환해서 폴더명을 만듦.
    하이픈이 포함된 디렉토리명과 구분하기 위해 실제 존재하는 경로를 탐욕적으로 탐색.
    """
    parts = folder_name.lstrip("-").split("-")
    if not parts:
        return "/" + folder_name.lstrip("-")

    # 탐욕법: 왼쪽부터 가장 긴 매칭을 시도하되, '-'와 '_' 구분자 모두 테스트
    best_segments = []
    i = 0
    while i < len(parts):
        found = False
        for j in range(len(parts), i, -1):
            sub = parts[i:j]
            # '-' 연결과 '_' 연결 모두 시도 (Claude Code는 _ 도 - 로 인코딩)
            for sep in ("-", "_"):
                candidate = sep.join(sub)
                test_path = "/" + "/".join(best_segments + [candidate])
                if os.path.exists(test_path):
                    best_segments.append(candidate)
                    i = j
                    found = True
                    break
            if found:
                break
        if not found:
            best_segments.append(parts[i])
            i += 1

    return "/" + "/".join(best_segments)


# ─── 데이터 수집 함수들 ──────────────────────────────────────────────────────

def get_health() -> dict:
    """하네스 건강 점수 (0-100), 7개 항목 체크."""
    items = {}

    # 1) claude_md
    items["claude_md"] = (CLAUDE_DIR / "CLAUDE.md").is_file()

    # 2) permissions — settings.json에 permissions 키가 있는지
    settings = read_json(CLAUDE_DIR / "settings.json")
    items["permissions"] = bool(settings and "permissions" in settings)

    # 3) hooks — settings.json에 hooks 키가 있는지
    items["hooks"] = bool(settings and "hooks" in settings)

    # 4) agents — 실제 에이전트가 있는지 (기본 경로 + 플러그인 캐시)
    items["agents"] = len(get_agents()["agents"]) > 0

    # 5) skills — 실제 스킬이 있는지 (기본 경로 + 플러그인 캐시)
    items["skills"] = len(get_skills()["skills"]) > 0

    # 6) connectors — MCP 서버가 있는지 (전체 스캔)
    items["connectors"] = len(get_connectors()["connectors"]) > 0

    # 7) plugins — installed_plugins.json이 있고 플러그인 항목이 있는지
    plugins_data = read_json(CLAUDE_DIR / "plugins" / "installed_plugins.json")
    if plugins_data and isinstance(plugins_data, dict):
        plugins_map = plugins_data.get("plugins", {})
        items["plugins"] = len(plugins_map) > 0
    else:
        items["plugins"] = False

    total = 100
    per_item = total // len(items)  # 14점씩, 나머지는 마지막에
    score = sum(per_item for v in items.values() if v)
    # 보정: 7개 모두 True면 100
    if all(items.values()):
        score = 100

    return {"score": score, "total": total, "items": items}


def get_sessions() -> dict:
    """현재 사용자의 활성 claude 프로세스 목록."""
    sessions = []
    try:
        user = os.environ.get("USER", os.getlogin())
    except OSError:
        user = "unknown"

    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C"}
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"sessions": sessions}

    # ps aux 헤더: USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND
    for line in result.stdout.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        ps_user, pid_str, _cpu, _mem, _vsz, _rss, tty, stat, start, time_field, command = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6], parts[7], parts[8], parts[9], parts[10]

        if ps_user != user:
            continue

        # claude 프로세스만 필터 (claude 바이너리 또는 node claude)
        cmd_lower = command.lower()
        if "claude" not in cmd_lower:
            continue
        # 보조 프로세스 제외 (MCP 서버, node, bash wrapper 등)
        if any(skip in cmd_lower for skip in [
            "mcp-server", "server.py", "claude-cockpit", "node ", "bridge",
            "/bin/bash", "cwd",
        ]):
            continue

        pid = int(pid_str)

        # 프로세스 상태 매핑
        if stat.startswith("T"):
            state = "stopped"
        elif stat.startswith("R"):
            state = "running"
        else:
            state = "active"  # S, Sl+ 등

        # cwd 가져오기
        cwd = ""
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except (FileNotFoundError, PermissionError, OSError):
            pass

        sessions.append({
            "pid": pid,
            "state": state,
            "tty": tty,
            "started": start,
            "command": command.split("/")[-1] if "/" in command else command,
            "cwd": cwd,
        })

    return {"sessions": sessions}


def get_activity() -> dict:
    """history.jsonl에서 오늘 명령 수 + 최근 활동 요약."""
    history_path = CLAUDE_DIR / "history.jsonl"
    if not history_path.is_file():
        return {"today_count": 0, "recent": []}

    now_ms = int(time.time() * 1000)
    today_start_ms = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    today_count = 0
    recent = []

    try:
        text = read_text(history_path)
        if not text:
            return {"today_count": 0, "recent": []}
        lines = text.splitlines()

        # 오늘 명령 수 (전체 스캔)
        for line in lines:
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", 0)
                if ts >= today_start_ms:
                    today_count += 1
            except Exception:
                continue

        # 최근 10개 (마지막부터 역순)
        for line in reversed(lines[-50:]):
            if len(recent) >= 10:
                break
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", 0)
                display = entry.get("display", "").strip()
                if not display:
                    continue
                project = entry.get("project", "")
                project_name = Path(project).name if project else ""
                age_seconds = max(0, (now_ms - ts) // 1000) if ts else 0
                recent.append({
                    "text": display[:100] + ("..." if len(display) > 100 else ""),
                    "project": project_name,
                    "ageSeconds": int(age_seconds),
                })
            except Exception:
                continue
    except Exception:
        pass

    return {"today_count": today_count, "recent": recent}


def _truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "..." if len(s) > max_len else s


def get_projects_summary() -> dict:
    """history.jsonl 기반 프로젝트별 명령 수 / 첫 요청 / 마지막 결과."""
    history_path = CLAUDE_DIR / "history.jsonl"
    if not history_path.is_file():
        return {"projects": []}

    by_project: dict = {}
    try:
        text = read_text(history_path)
        if not text:
            return {"projects": []}

        for line in text.splitlines()[-5000:]:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            proj = entry.get("project")
            ts = entry.get("timestamp")
            if not proj or not isinstance(ts, (int, float)):
                continue
            display = (entry.get("display") or "").strip()

            slot = by_project.setdefault(proj, {
                "name": Path(proj).name or proj,
                "cwd": proj,
                "command_count": 0,
                "last_activity_ms": 0,
                "first_request": "",
                "first_ts": 0,
                "last_result": "",
            })
            slot["command_count"] += 1
            if ts > slot["last_activity_ms"]:
                slot["last_activity_ms"] = ts
                if display:
                    slot["last_result"] = display[:160]
            if slot["first_ts"] == 0 or ts < slot["first_ts"]:
                slot["first_ts"] = ts
                if display:
                    slot["first_request"] = display[:160]
    except Exception:
        pass

    now_ms = int(time.time() * 1000)
    result = []
    for v in by_project.values():
        age = max(0, (now_ms - v["last_activity_ms"]) // 1000) if v["last_activity_ms"] else 0
        result.append({
            "name": v["name"],
            "cwd": v["cwd"],
            "command_count": v["command_count"],
            "last_activity_ago": int(age),
            "first_request": v["first_request"],
            "last_result": v["last_result"],
        })
        v.pop("first_ts", None)

    result.sort(key=lambda x: x["last_activity_ago"])
    return {"projects": result[:20]}


def get_instructions() -> dict:
    """CLAUDE.md 파일들 (글로벌 + 프로젝트별)."""
    result: dict[str, Any] = {"global": {"exists": False}, "projects": []}
    max_content = 50000  # 전체 내용 표시

    # 글로벌 CLAUDE.md
    global_path = CLAUDE_DIR / "CLAUDE.md"
    if global_path.is_file():
        content = read_text(global_path)
        if content is not None:
            size = global_path.stat().st_size
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            truncated = len(content) > max_content
            result["global"] = {
                "exists": True,
                "content": content[:max_content],
                "size": size,
                "line_count": line_count,
                "truncated": truncated,
            }

    # 프로젝트별 CLAUDE.md
    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.is_dir():
        # 등록된 모든 프로젝트 경로 수집 (하위 프로젝트 필터링용)
        all_project_paths = []
        for pd in projects_dir.iterdir():
            if pd.is_dir():
                all_project_paths.append(decode_project_path(pd.name))
        all_project_paths.sort(key=len, reverse=True)  # 긴 경로(더 구체적) 먼저
        seen_files = set()  # full_path 중복 방지

        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            claude_md = proj_dir / "CLAUDE.md"
            decoded_path = decode_project_path(proj_dir.name)
            if claude_md.is_file():
                content = read_text(claude_md)
                if content is not None:
                    size = claude_md.stat().st_size
                    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                    truncated = len(content) > max_content
                    result["projects"].append({
                        "path": decoded_path,
                        "content": content[:max_content],
                        "size": size,
                        "line_count": line_count,
                        "truncated": truncated,
                    })
            # 프로젝트 디렉토리 내 CLAUDE.md 트리 스캔 (루트 + 하위)
            project_root = Path(decoded_path)
            if project_root.is_dir():
                # 이 프로젝트 하위에 등록된 다른 프로젝트 경로 (그쪽은 건너뛰기)
                sub_projects = [p for p in all_project_paths
                                if p != decoded_path and p.startswith(decoded_path + "/")]
                try:
                    for md_file in project_root.rglob("CLAUDE.md"):
                        md_str = str(md_file)
                        # 중복 방지
                        if md_str in seen_files:
                            continue
                        # .git 등 숨김 디렉토리 제외
                        if any(p.startswith(".") for p in md_file.relative_to(project_root).parts[:-1]):
                            continue
                        # 다른 등록된 프로젝트 영역이면 건너뛰기
                        parent_dir = str(md_file.parent)
                        skip = False
                        for sp in sub_projects:
                            if parent_dir.startswith(sp):
                                skip = True
                                break
                        if skip:
                            continue
                        seen_files.add(md_str)
                        content = read_text(md_file)
                        if content is None:
                            continue
                        size = md_file.stat().st_size
                        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                        truncated = len(content) > max_content
                        rel = str(md_file.relative_to(project_root).parent)
                        sub_label = "" if rel == "." else rel
                        result["projects"].append({
                            "path": decoded_path,
                            "sub_path": sub_label,
                            "full_path": str(md_file),
                            "local": True,
                            "content": content[:max_content],
                            "size": size,
                            "line_count": line_count,
                            "truncated": truncated,
                        })
                except (PermissionError, OSError):
                    pass

    return result


def get_skills() -> dict:
    """스킬 스캔: ~/.claude/skills/ + 플러그인 캐시 내 skills/."""
    skills = []
    seen = set()

    def _scan_skills_dir(skills_dir: Path, source: str = "user"):
        if not skills_dir.is_dir():
            return
        for d in sorted(skills_dir.iterdir()):
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.is_file():
                continue
            content = read_text(skill_md)
            if content is None:
                continue
            fm = parse_frontmatter(content)
            name = fm.get("name", d.name)
            if name in seen:
                continue
            seen.add(name)
            # frontmatter 이후 본문 추출
            body = content
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    body = content[end + 3:].strip()
            skills.append({
                "name": name,
                "description": fm.get("description", ""),
                "path": str(skill_md),
                "source": source,
                "content": body[:2000],
            })

    # 기본 경로
    _scan_skills_dir(CLAUDE_DIR / "skills")

    # 플러그인 캐시 내 스킬 — 플러그인 이름 추출
    plugins_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugins_cache.is_dir():
        for skills_subdir in plugins_cache.rglob("skills"):
            if skills_subdir.is_dir():
                # 경로에서 플러그인 이름 추출: cache/<org>/<plugin-name>/<ver>/skills
                parts = skills_subdir.relative_to(plugins_cache).parts
                plugin_name = parts[1] if len(parts) >= 2 else "plugin"
                _scan_skills_dir(skills_subdir, source=f"plugin:{plugin_name}")

    return {"skills": skills}


def get_agents() -> dict:
    """에이전트 스캔: ~/.claude/agents/ + 플러그인 캐시 내 agents/."""
    agents = []
    seen = set()

    def _scan_agents_dir(agents_dir: Path, source: str = "user"):
        if not agents_dir.is_dir():
            return
        for f in sorted(agents_dir.glob("*.md")):
            content = read_text(f)
            if content is None:
                continue
            fm = parse_frontmatter(content)
            name = fm.get("name", f.stem)
            if name in seen:
                continue
            seen.add(name)
            tools = fm.get("tools", [])
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            body = content
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    body = content[end + 3:].strip()
            agents.append({
                "name": name,
                "description": fm.get("description", ""),
                "model": fm.get("model", ""),
                "tools": tools,
                "source": source,
                "content": body[:2000],
            })

    # 기본 경로
    _scan_agents_dir(CLAUDE_DIR / "agents")

    # 플러그인 캐시 내 에이전트
    plugins_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugins_cache.is_dir():
        for d in plugins_cache.rglob("agents"):
            if d.is_dir():
                parts = d.relative_to(plugins_cache).parts
                plugin_name = parts[1] if len(parts) >= 2 else "plugin"
                _scan_agents_dir(d, source=f"plugin:{plugin_name}")

    return {"agents": agents}


def get_connectors() -> dict:
    """MCP 서버 스캔: ~/.claude.json + 플러그인 내 .mcp.json + 세션 JSONL에서 cloud MCP 추출."""
    connectors = []
    seen = set()  # "<source>:<name>" keys already added

    def _add_mcp_servers(data: dict, source: str = "", plugin_name: str = ""):
        if not data or "mcpServers" not in data:
            return
        mcp_servers = data["mcpServers"]
        if not isinstance(mcp_servers, dict):
            return
        for name, config in sorted(mcp_servers.items()):
            if not isinstance(config, dict):
                continue
            key = f"{source}:{name}" if source else name
            if key in seen:
                continue
            seen.add(key)
            # 플러그인 서버의 경우 표시명에 플러그인 이름 포함
            display_name = name
            if plugin_name and name != plugin_name:
                display_name = f"{plugin_name} ({name})"
            connectors.append({
                "name": display_name,
                "_mcp_name": name,          # 실제 MCP 서버 이름 (매칭용)
                "_plugin_name": plugin_name, # 플러그인 이름 (매칭용)
                "command": config.get("command", ""),
                "type": config.get("type", "local"),
                "args": config.get("args", []),
                "source": source,
                "tools": [],
                "tool_count": 0,
            })

    # ~/.claude.json
    _add_mcp_servers(read_json(CLAUDE_JSON), "global")

    # 플러그인 내 .mcp.json 파일들 — 중복 방지를 위해 cache만 스캔 (marketplaces와 동일)
    plugins_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugins_cache.is_dir():
        for mcp_file in plugins_cache.rglob(".mcp.json"):
            data = read_json(mcp_file)
            # 경로에서 플러그인 이름 추출: cache/<org>/<plugin-name>/<ver>/.mcp.json
            parts = mcp_file.relative_to(plugins_cache).parts
            plugin_name = parts[1] if len(parts) >= 2 else mcp_file.parent.name
            _add_mcp_servers(data, f"plugin:{plugin_name}", plugin_name)

    # ── 2차 패스: 세션 JSONL에서 mcp__ 패턴 추출 (cloud + plugin MCP 서버 발견) ──

    # 유효한 MCP 도구명 패턴: mcp__<alphanum_hyphen>__<alphanum_hyphen>
    _VALID_MCP_RE = re.compile(r"^mcp__[A-Za-z0-9][A-Za-z0-9_-]*__[A-Za-z][A-Za-z0-9_-]*$")

    def _parse_mcp_prefix(prefix: str):
        """mcp server prefix를 (provider, server_name, mcp_type, source)로 분해.

        Examples:
          claude_ai_Atlassian -> ('claude_ai', 'Atlassian', 'cloud', 'claude.ai')
          plugin_oh-my-claudecode_t -> ('plugin', 'oh-my-claudecode', 'local', 'plugin:oh-my-claudecode')
        """
        parts = prefix.split("_")
        # 하이픈을 포함하는 세그먼트가 있으면 플러그인 형식
        for i, p in enumerate(parts):
            if "-" in p:
                provider = "_".join(parts[:i]) if i > 0 else "plugin"
                server = p
                return provider, server, "local", f"plugin:{server}"
        # 하이픈 없음: 마지막 세그먼트가 서버 이름, 나머지가 프로바이더
        if len(parts) >= 2:
            server = parts[-1]
            provider = "_".join(parts[:-1])
            return provider, server, "cloud", "claude.ai"
        return prefix, prefix, "cloud", "claude.ai"

    # 활성 세션 JSONL 파일 수집 (sessions/*.json → sessionId → projects/**/<sessionId>.jsonl)
    sessions_dir = CLAUDE_DIR / "sessions"
    jsonl_paths: set = set()
    if sessions_dir.is_dir():
        projects_dir = CLAUDE_DIR / "projects"
        for sess_file in sessions_dir.glob("*.json"):
            sess_data = read_json(sess_file)
            if not sess_data or not isinstance(sess_data, dict):
                continue
            session_id = sess_data.get("sessionId", "")
            if not session_id:
                continue
            if projects_dir.is_dir():
                for jsonl_file in projects_dir.rglob(f"{session_id}.jsonl"):
                    jsonl_paths.add(jsonl_file)

    # JSONL이 없으면 모든 프로젝트의 최근 JSONL 파일도 포함 (fallback)
    if not jsonl_paths:
        projects_dir = CLAUDE_DIR / "projects"
        if projects_dir.is_dir():
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl_file in proj_dir.glob("*.jsonl"):
                    if "subagent" not in jsonl_file.name:
                        jsonl_paths.add(jsonl_file)

    # 각 JSONL에서 "mcp__" 패턴 grep으로 추출
    # server_name -> {tools: set, type, source, command}
    server_tools: dict = {}
    for jsonl_path in jsonl_paths:
        try:
            result = subprocess.run(
                ["grep", "-o", '"mcp__[^"]*"', str(jsonl_path)],
                capture_output=True, text=True, timeout=10,
            )
            if not result.stdout:
                continue
            for raw in result.stdout.splitlines():
                tool_name = raw.strip().strip('"')
                # 엄격한 유효성 검사: 올바른 mcp__ 패턴만 허용
                if not _VALID_MCP_RE.match(tool_name):
                    continue
                # mcp__<prefix>__<tool>
                inner = tool_name[5:]  # strip "mcp__"
                sep_idx = inner.find("__")
                if sep_idx == -1:
                    continue
                prefix = inner[:sep_idx]
                tool = inner[sep_idx + 2:]
                if not prefix or not tool:
                    continue
                _, server_name, mcp_type, source = _parse_mcp_prefix(prefix)
                if server_name not in server_tools:
                    server_tools[server_name] = {
                        "tools": set(),
                        "type": mcp_type,
                        "source": source,
                        "command": "cloud" if mcp_type == "cloud" else "node",
                    }
                server_tools[server_name]["tools"].add(tool)
        except Exception:
            continue

    # 로컬 스캔 결과와 병합
    for server_name, info in sorted(server_tools.items()):
        tools_list = sorted(info["tools"])
        tool_count = len(tools_list)

        # 매칭 우선순위:
        # 1) 이름 직접 일치
        # 2) 같은 플러그인 소스 (e.g. plugin:oh-my-claudecode)로 등록된 서버
        existing = next((c for c in connectors if c.get("_mcp_name") == server_name), None)
        if existing is None and info["source"].startswith("plugin:"):
            existing = next(
                (c for c in connectors if c.get("source") == info["source"]),
                None,
            )
        if existing:
            # tools 목록만 업데이트
            existing["tools"] = tools_list
            existing["tool_count"] = tool_count
        else:
            # 새 서버 추가 (cloud MCP 또는 jsonl에서만 발견된 plugin)
            key = f"{info['source']}:{server_name}"
            if key in seen:
                continue
            seen.add(key)
            connectors.append({
                "name": server_name,
                "_mcp_name": server_name,
                "_plugin_name": "",
                "command": info["command"],
                "type": info["type"],
                "args": [],
                "source": info["source"],
                "tools": tools_list,
                "tool_count": tool_count,
            })

    # 내부 매칭 키 제거 (API 응답에 불필요)
    for c in connectors:
        c.pop("_mcp_name", None)
        c.pop("_plugin_name", None)

    return {"connectors": connectors}


def get_hooks() -> dict:
    """~/.claude/settings.json → hooks."""
    hooks_list = []
    settings = read_json(CLAUDE_DIR / "settings.json")
    if not settings or "hooks" not in settings:
        return {"hooks": hooks_list}

    hooks = settings["hooks"]
    if isinstance(hooks, dict):
        for event, handlers in hooks.items():
            if not isinstance(handlers, list):
                handlers = [handlers]
            for h in handlers:
                if not isinstance(h, dict):
                    continue
                matcher = h.get("matcher", "")
                # 새 구조: {"matcher": "...", "hooks": [{...}]}
                sub_hooks = h.get("hooks", [])
                if isinstance(sub_hooks, list) and sub_hooks:
                    for sh in sub_hooks:
                        if isinstance(sh, dict):
                            entry = {
                                "event": event,
                                "matcher": matcher,
                                "type": sh.get("type", "command"),
                                "command": sh.get("command", ""),
                                "prompt": sh.get("prompt", ""),
                                "url": sh.get("url", ""),
                                "description": sh.get("description", ""),
                                "statusMessage": sh.get("statusMessage", ""),
                                "timeout": sh.get("timeout"),
                                "model": sh.get("model", ""),
                                "once": sh.get("once", False),
                                "async": sh.get("async", False),
                                "asyncRewake": sh.get("asyncRewake", False),
                                "if": sh.get("if", ""),
                                "shell": sh.get("shell", ""),
                            }
                            # 빈 값 제거
                            entry = {k: v for k, v in entry.items() if v}
                            entry.setdefault("event", event)
                            entry.setdefault("type", "command")
                            hooks_list.append(entry)
                # 이전 구조: {"command": "...", "description": "..."}
                elif "command" in h:
                    hooks_list.append({
                        "event": event,
                        "matcher": "",
                        "type": "command",
                        "command": h["command"],
                        "description": h.get("description", ""),
                        "timeout": h.get("timeout"),
                    })

    # user hooks에 source 태그 추가
    for h in hooks_list:
        if "source" not in h:
            h["source"] = "user"

    # 플러그인 hooks 스캔
    plugins_dir = CLAUDE_DIR / "plugins"
    if plugins_dir.is_dir():
        for hooks_json in plugins_dir.rglob("hooks/hooks.json"):
            try:
                data = json.loads(hooks_json.read_text(encoding="utf-8"))
                plugin_hooks = data.get("hooks", {})
                # 플러그인 이름 추출
                parts = str(hooks_json).split("/")
                plugin_name = "plugin"
                for i, p in enumerate(parts):
                    if p == "cache" and i + 3 < len(parts):
                        plugin_name = parts[i + 2]
                        break
                    if p == "plugins" and i + 2 < len(parts) and parts[i + 1] != "cache":
                        plugin_name = parts[i + 2] if parts[i + 1] == "marketplaces" else parts[i + 1]
                        break

                if isinstance(plugin_hooks, dict):
                    for event, handlers in plugin_hooks.items():
                        if not isinstance(handlers, list):
                            handlers = [handlers]
                        for h in handlers:
                            if not isinstance(h, dict):
                                continue
                            sub_hooks = h.get("hooks", [])
                            matcher = h.get("matcher", "")
                            for sh in (sub_hooks if isinstance(sub_hooks, list) else []):
                                if not isinstance(sh, dict):
                                    continue
                                entry = {
                                    "event": event,
                                    "matcher": matcher,
                                    "type": sh.get("type", "command"),
                                    "command": sh.get("command", ""),
                                    "description": sh.get("description", sh.get("statusMessage", "")),
                                    "timeout": sh.get("timeout"),
                                    "source": f"plugin:{plugin_name}",
                                }
                                entry = {k: v for k, v in entry.items() if v}
                                entry.setdefault("event", event)
                                entry.setdefault("type", "command")
                                entry.setdefault("source", f"plugin:{plugin_name}")
                                hooks_list.append(entry)
            except Exception:
                continue

    return {"hooks": hooks_list}


def get_forks() -> dict:
    """JSONL 세션 파일에서 포크 포인트(분기) 탐색."""
    forks = []
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"forks": [], "total": 0}

    seen_texts = set()

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if "subagent" in str(jsonl_file):
                continue

            try:
                text = read_text(jsonl_file)
                if not text:
                    continue
                lines = text.splitlines()
                # 마지막 3000줄만 처리
                lines = lines[-3000:]

                # parent→children 맵 구축
                parent_to_children: dict[str, list[dict]] = {}
                entries_by_uuid: dict[str, dict] = {}

                for line in lines:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    uuid = entry.get("uuid")
                    parent_uuid = entry.get("parentUuid")
                    if uuid:
                        entries_by_uuid[uuid] = entry
                    if parent_uuid:
                        parent_to_children.setdefault(parent_uuid, []).append(entry)

                # 포크 포인트 찾기: children이 2개 이상인 parent
                project_name = decode_project_path(proj_dir.name)
                session_id = jsonl_file.stem

                for parent_uuid, children in parent_to_children.items():
                    if len(children) <= 1:
                        continue
                    # 각 분기 자식 중 user 메시지 추출
                    for child in children:
                        if child.get("type") != "user":
                            continue
                        msg_text = child.get("message", {}).get("content", "") if isinstance(child.get("message"), dict) else ""
                        if not msg_text and isinstance(child.get("content"), str):
                            msg_text = child["content"]
                        if not msg_text:
                            # content가 리스트인 경우
                            content = child.get("message", {}).get("content", []) if isinstance(child.get("message"), dict) else child.get("content", [])
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        msg_text = block.get("text", "")
                                        break
                                    elif isinstance(block, str):
                                        msg_text = block
                                        break

                        if not msg_text or len(msg_text) < 5:
                            continue
                        if msg_text.startswith("{"):
                            continue

                        display_text = msg_text[:100]
                        dedup_key = msg_text[:50]
                        if dedup_key in seen_texts:
                            continue
                        seen_texts.add(dedup_key)

                        ts = child.get("timestamp", "")

                        forks.append({
                            "text": display_text,
                            "project": project_name,
                            "session": session_id,
                            "timestamp": ts,
                        })
            except Exception:
                continue

    # timestamp 내림차순 정렬
    forks.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    return {"forks": forks, "total": len(forks)}


def get_project_status() -> dict:
    """프로젝트별 관리 상태 스캔: CLAUDE.md, memory, settings, sessions 등."""
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"projects": []}

    result = []
    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue

        decoded_path = decode_project_path(proj_dir.name)
        name = Path(decoded_path).name or proj_dir.name

        # 1) CLAUDE.md in project config
        claude_md_path = proj_dir / "CLAUDE.md"
        project_claude_md = claude_md_path.is_file()
        project_claude_md_size = 0
        if project_claude_md:
            try:
                project_claude_md_size = claude_md_path.stat().st_size
            except OSError:
                pass

        # 2) Memory files
        memory_dir = proj_dir / "memory"
        memory_count = 0
        if memory_dir.is_dir():
            try:
                memory_count = sum(1 for f in memory_dir.iterdir() if f.suffix == ".md")
            except OSError:
                pass

        # 3) settings.local.json
        has_settings = (proj_dir / "settings.local.json").is_file()

        # 4) Session count (.jsonl files, not in subagent dirs)
        session_count = 0
        try:
            for f in proj_dir.glob("*.jsonl"):
                if "subagent" not in f.name:
                    session_count += 1
        except OSError:
            pass

        # 5) Local CLAUDE.md (in the decoded project path)
        local_claude_md = Path(decoded_path, "CLAUDE.md").is_file()

        # 6) Local .claude/ directory
        has_local_claude_dir = Path(decoded_path, ".claude").is_dir()

        result.append({
            "name": name,
            "path": decoded_path,
            "raw_dir": proj_dir.name,
            "project_claude_md": project_claude_md,
            "project_claude_md_size": project_claude_md_size,
            "local_claude_md": local_claude_md,
            "memory_count": memory_count,
            "has_settings": has_settings,
            "session_count": session_count,
            "has_local_claude_dir": has_local_claude_dir,
        })

    # Sort by session_count descending
    result.sort(key=lambda x: x["session_count"], reverse=True)
    return {"projects": result}


def get_plugins() -> dict:
    """installed_plugins.json + settings.json enabledPlugins."""
    plugins = []

    # 설치된 플러그인 읽기
    plugins_data = read_json(CLAUDE_DIR / "plugins" / "installed_plugins.json")
    if not plugins_data or not isinstance(plugins_data, dict):
        return {"plugins": plugins}

    plugins_map = plugins_data.get("plugins", {})
    if not isinstance(plugins_map, dict):
        return {"plugins": plugins}

    # 활성화된 플러그인 목록
    settings = read_json(CLAUDE_DIR / "settings.json")
    enabled_plugins = {}
    if settings and "enabledPlugins" in settings:
        ep = settings["enabledPlugins"]
        if isinstance(ep, dict):
            enabled_plugins = ep  # {"name@scope": true/false}
        elif isinstance(ep, list):
            enabled_plugins = {name: True for name in ep}

    for plugin_key, entries in plugins_map.items():
        # entries는 설치 정보 리스트
        if isinstance(entries, list) and entries:
            entry = entries[0]  # 첫 번째 설치 정보
            if isinstance(entry, dict):
                # 이름 추출: "oh-my-claudecode@omc" → "oh-my-claudecode"
                name = plugin_key.split("@")[0] if "@" in plugin_key else plugin_key
                # 플러그인 소속 스킬/에이전트/커넥터 수
                plugin_source = f"plugin:{name}"
                n_skills = len([s for s in get_skills()["skills"] if s.get("source") == plugin_source])
                n_agents = len([a for a in get_agents()["agents"] if a.get("source") == plugin_source])
                n_connectors = len([c for c in get_connectors()["connectors"] if c.get("source") == plugin_source])

                plugins.append({
                    "name": name,
                    "version": entry.get("version", "unknown"),
                    "enabled": bool(enabled_plugins.get(plugin_key, False)),
                    "scope": entry.get("scope", ""),
                    "install_path": entry.get("installPath", ""),
                    "installed_at": entry.get("installedAt", ""),
                    "last_updated": entry.get("lastUpdated", ""),
                    "git_sha": entry.get("gitCommitSha", "")[:8],
                    "skills_count": n_skills,
                    "agents_count": n_agents,
                    "connectors_count": n_connectors,
                })

    return {"plugins": plugins}


# ─── 세션 상세 / 검색 ────────────────────────────────────────────────────────

def _read_last_n_lines(path: Path, n: int) -> list[str]:
    """파일의 마지막 N줄을 효율적으로 읽기."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return []
            # JSONL은 줄당 수 KB일 수 있으므로 넉넉히 읽기
            read_size = min(fsize, n * 5000)
            f.seek(max(0, fsize - read_size))
            data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            return lines[-n:] if len(lines) > n else lines
    except (FileNotFoundError, PermissionError, OSError):
        return []


def _read_first_n_lines(path: Path, n: int) -> list[str]:
    """파일의 처음 N줄을 읽기."""
    try:
        lines = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= n:
                    break
                lines.append(line.rstrip("\n"))
        return lines
    except (FileNotFoundError, PermissionError, OSError):
        return []


def _extract_user_message_text(entry: dict) -> str:
    """JSONL 엔트리에서 사용자 메시지 텍스트 추출."""
    if entry.get("type") != "user":
        return ""
    msg = entry.get("message", {})
    if isinstance(msg, dict):
        content = msg.get("content", "")
    else:
        content = entry.get("content", "")

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
            elif isinstance(block, str):
                return block
    return ""


def get_session_detail() -> dict:
    """모든 세션(활성 + 히스토리) 상세 정보."""
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"sessions": []}

    # 활성 세션 ID 수집: sessions/*.json에서 PID가 실행 중인 세션의 sessionId
    active_sessions = get_sessions().get("sessions", [])
    active_pids = {s["pid"] for s in active_sessions if s["state"] != "stopped"}
    active_session_ids = set()
    sessions_dir = CLAUDE_DIR / "sessions"
    if sessions_dir.is_dir():
        for sf in sessions_dir.glob("*.json"):
            try:
                sd = json.loads(read_text(sf) or "{}")
                pid = sd.get("pid")
                sid = sd.get("sessionId")
                if pid in active_pids and sid:
                    active_session_ids.add(sid)
            except Exception:
                continue

    sessions = []

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if "subagent" in str(jsonl_file):
                continue

            try:
                session_id = jsonl_file.stem
                file_size = jsonl_file.stat().st_size
                file_size_kb = round(file_size / 1024, 1)

                decoded_path = decode_project_path(proj_dir.name)
                project_name = Path(decoded_path).name or proj_dir.name

                # 마지막 5줄에서 slug 추출
                last_5 = _read_last_n_lines(jsonl_file, 5)
                slug = ""
                last_timestamp = ""
                custom_title = ""
                for line in reversed(last_5):
                    try:
                        entry = json.loads(line)
                        if not slug and entry.get("slug"):
                            slug = entry["slug"]
                        if not last_timestamp and entry.get("timestamp"):
                            last_timestamp = entry["timestamp"]
                    except Exception:
                        continue
                # customTitle은 type:"custom-title" 엔트리에만 존재 — grep으로 찾기
                if not custom_title:
                    try:
                        result = subprocess.run(
                            ["grep", "-o", '"customTitle":"[^"]*"', str(jsonl_file)],
                            capture_output=True, text=True, timeout=3
                        )
                        if result.stdout.strip():
                            last_match = result.stdout.strip().splitlines()[-1]
                            custom_title = last_match.split(':"')[1].rstrip('"')
                    except Exception:
                        pass
                slug = custom_title or slug

                # 메시지 수 추정 (마지막 500줄 샘플링)
                last_500 = _read_last_n_lines(jsonl_file, 500)
                message_count = 0
                for line in last_500:
                    try:
                        entry = json.loads(line)
                        t = entry.get("type", "")
                        if t in ("user", "assistant"):
                            message_count += 1
                    except Exception:
                        continue

                # 첫 번째 사용자 메시지 (처음 20줄)
                first_20 = _read_first_n_lines(jsonl_file, 20)
                first_message = ""
                for line in first_20:
                    try:
                        entry = json.loads(line)
                        text = _extract_user_message_text(entry)
                        if text and len(text) >= 3 and not text.startswith("{"):
                            first_message = text[:120]
                            break
                    except Exception:
                        continue

                # 마지막 사용자 메시지 (마지막 20줄)
                last_20 = _read_last_n_lines(jsonl_file, 20)
                last_message = ""
                for line in reversed(last_20):
                    try:
                        entry = json.loads(line)
                        text = _extract_user_message_text(entry)
                        if text and len(text) >= 3 and not text.startswith("{"):
                            last_message = text[:120]
                            break
                    except Exception:
                        continue

                # 포크 수 (마지막 1000줄 샘플링)
                last_1000 = _read_last_n_lines(jsonl_file, 1000)
                parent_children: dict[str, int] = {}
                for line in last_1000:
                    try:
                        entry = json.loads(line)
                        parent_uuid = entry.get("parentUuid")
                        if parent_uuid:
                            parent_children[parent_uuid] = parent_children.get(parent_uuid, 0) + 1
                    except Exception:
                        continue
                fork_count = sum(1 for c in parent_children.values() if c > 1)

                # 활성 여부 확인 (세션 ID가 활성 세션 목록에 있는지)
                is_active = session_id in active_session_ids

                sessions.append({
                    "session_id": session_id,
                    "slug": slug,
                    "project": decoded_path,
                    "project_name": project_name,
                    "file_size_kb": file_size_kb,
                    "message_count": message_count,
                    "first_message": first_message,
                    "last_message": last_message,
                    "last_timestamp": last_timestamp,
                    "is_active": is_active,
                    "fork_count": fork_count,
                })
            except Exception:
                continue

    # 정렬: 활성 먼저, 그 다음 last_timestamp 내림차순
    sessions.sort(key=lambda x: (not x["is_active"], -(x.get("last_timestamp") or 0) if isinstance(x.get("last_timestamp"), (int,float)) else x.get("last_timestamp", "") or ""), reverse=False)

    return {"sessions": sessions}


def _extract_token_usage(lines: list[str]) -> dict:
    """JSONL 줄 목록에서 마지막 assistant 메시지의 usage.cache_read_input_tokens를 추출.

    Returns dict with 'context_tokens' (int) or empty dict if not found.
    """
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            # usage가 message 안에 있을 수도 있음
            msg = entry.get("message")
            if isinstance(msg, dict):
                usage = msg.get("usage")
        if isinstance(usage, dict) and "cache_read_input_tokens" in usage:
            return {
                "context_tokens": usage["cache_read_input_tokens"],
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            }
    return {}


def _format_token_display(tokens: int) -> str:
    """토큰 수를 읽기 쉬운 문자열로 포맷. 예: 318029 → '318k'"""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    elif tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


def get_session_xray(session_id: str) -> dict:
    """세션 컨텍스트 X-ray: JSONL의 실제 토큰 사용량(cache_read_input_tokens) 기반."""
    if not session_id:
        return {"error": "session_id required"}

    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"error": "projects dir not found"}

    # Find the jsonl file for this session
    jsonl_path = None
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            jsonl_path = candidate
            break

    if not jsonl_path:
        return {"error": f"session {session_id} not found"}

    # Read last 20 lines to find real token usage
    last_20 = _read_last_n_lines(jsonl_path, 20)
    token_data = _extract_token_usage(last_20)

    context_max = 1_000_000  # 1M context model
    context_tokens = token_data.get("context_tokens", 0)
    context_pct = round(context_tokens / context_max * 100) if context_max > 0 else 0

    # Count compacts and messages since last compact from last 500 lines
    last_500 = _read_last_n_lines(jsonl_path, 500)
    compacts_total = 0
    last_compact_timestamp = None
    messages_since_compact = 0
    found_compact = False

    # First pass: count all compacts (need full file for total count)
    # Read entire file just for compact counting (type=="summary" lines are rare and small)
    try:
        with open(jsonl_path, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
        for raw_line in raw.splitlines():
            try:
                entry = json.loads(raw_line)
            except Exception:
                continue
            if entry.get("type") == "summary":
                compacts_total += 1
                last_compact_timestamp = entry.get("timestamp")
    except (FileNotFoundError, PermissionError, OSError):
        pass

    # Second pass on last 500: messages since last compact
    for line in reversed(last_500):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        entry_type = entry.get("type", "")
        if entry_type == "summary":
            found_compact = True
            break
        if entry_type in ("user", "assistant"):
            messages_since_compact += 1

    # If no compact found in last 500 lines but compacts exist, messages_since_compact
    # is approximate (lower bound from the 500-line window)

    # Format display string
    context_display = f"{_format_token_display(context_tokens)} / 1M tokens"

    # Breakdown 추정: 파일 크기 기반
    breakdown = []
    autocompact_buffer = 33000  # ~33k tokens (roughly fixed)

    # System prompt + tools (~15k fixed)
    system_tokens = 15000
    breakdown.append({"name": "System (prompt + tools)", "tokens": system_tokens})

    # CLAUDE.md + memory files
    memory_tokens = 0
    claude_md = CLAUDE_DIR / "CLAUDE.md"
    if claude_md.is_file():
        memory_tokens += claude_md.stat().st_size // 4
    memory_dir = CLAUDE_DIR / "projects"
    if memory_dir.is_dir():
        for md in memory_dir.rglob("memory/*.md"):
            try:
                memory_tokens += md.stat().st_size // 4
            except Exception:
                pass
    breakdown.append({"name": "Memory files", "tokens": memory_tokens})

    # Custom agents
    agent_tokens = 0
    agents_data = get_agents().get("agents", [])
    agent_tokens = len(agents_data) * 35  # ~35 tokens per agent definition
    breakdown.append({"name": "Custom agents", "tokens": agent_tokens})

    # Skills
    skills_data = get_skills().get("skills", [])
    skill_tokens = len(skills_data) * 22  # ~22 tokens per skill
    breakdown.append({"name": "Skills", "tokens": skill_tokens})

    # Messages = total - overhead
    overhead = system_tokens + memory_tokens + agent_tokens + skill_tokens
    message_tokens = max(0, context_tokens - overhead)
    breakdown.append({"name": "Messages", "tokens": message_tokens})

    # Free space
    free_tokens = max(0, context_max - context_tokens - autocompact_buffer)
    breakdown.append({"name": "Free space", "tokens": free_tokens})
    breakdown.append({"name": "Autocompact buffer", "tokens": autocompact_buffer})

    # Add pct to each
    for b in breakdown:
        b["pct"] = round(b["tokens"] / context_max * 100, 1)
        b["display"] = _format_token_display(b["tokens"])

    # Generate recommendation based on real token percentage
    if context_pct > 80:
        recommendation = f"Context nearly full ({context_pct}%). Use /compact or /handoff immediately"
    elif context_pct > 60:
        recommendation = f"Context getting large ({context_pct}%). Consider /compact soon"
    elif context_pct > 40:
        recommendation = f"Context moderate ({context_pct}%). Healthy for now"
    else:
        recommendation = f"Context healthy ({context_pct}%)"

    return {
        "session_id": session_id,
        "context_tokens": context_tokens,
        "context_max": context_max,
        "context_pct": context_pct,
        "context_display": context_display,
        "breakdown": breakdown,
        "messages_since_compact": messages_since_compact,
        "last_compact_timestamp": last_compact_timestamp,
        "compacts_total": compacts_total,
        "recommendation": recommendation,
    }


def get_session_search(query: str = "") -> dict:
    """세션 검색: JSONL 파일에서 사용자 메시지를 검색."""
    if not query or len(query.strip()) < 1:
        return {"results": [], "query": query}

    query_lower = query.lower().strip()
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"results": [], "query": query}

    results = []

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if "subagent" in str(jsonl_file):
                continue

            try:
                session_id = jsonl_file.stem
                decoded_path = decode_project_path(proj_dir.name)
                project_name = Path(decoded_path).name or proj_dir.name

                # slug/customTitle 추출
                last_5 = _read_last_n_lines(jsonl_file, 5)
                slug = ""
                custom_title = ""
                for line in reversed(last_5):
                    try:
                        entry = json.loads(line)
                        if not slug and entry.get("slug"):
                            slug = entry["slug"]
                    except Exception:
                        continue
                # customTitle은 type:"custom-title" 엔트리에만 존재 — grep으로 찾기
                if not custom_title:
                    try:
                        result = subprocess.run(
                            ["grep", "-o", '"customTitle":"[^"]*"', str(jsonl_file)],
                            capture_output=True, text=True, timeout=3
                        )
                        if result.stdout.strip():
                            last_match = result.stdout.strip().splitlines()[-1]
                            custom_title = last_match.split(':"')[1].rstrip('"')
                    except Exception:
                        pass
                slug = custom_title or slug

                # 마지막 2000줄 검색
                lines = _read_last_n_lines(jsonl_file, 2000)
                for line in reversed(lines):
                    try:
                        entry = json.loads(line)
                        text = _extract_user_message_text(entry)
                        if text and query_lower in text.lower():
                            ts = entry.get("timestamp", "")
                            results.append({
                                "session_id": session_id,
                                "slug": slug,
                                "project": project_name,
                                "matched_text": text[:150],
                                "timestamp": ts,
                            })
                            break  # 세션당 첫 매치만
                    except Exception:
                        continue

                if len(results) >= 20:
                    break
            except Exception:
                continue
        if len(results) >= 20:
            break

    return {"results": results[:20], "query": query}


# ─── 알림/모니터링 ─────────────────────────────────────────────────────────────

def _count_lines(path: Path) -> int:
    """파일의 줄 수를 반환."""
    try:
        content = read_text(path)
        if content is None:
            return 0
        return content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    except Exception:
        return 0


def get_alerts() -> dict:
    """경고/모니터링 정보 집계: CLAUDE.md 상태, 세션 컨텍스트 등."""
    alerts: list[dict] = []

    # ── 1. CLAUDE.md 체크 ──────────────────────────────────────────────
    # 1a. Global CLAUDE.md
    global_md = CLAUDE_DIR / "CLAUDE.md"
    if not global_md.is_file():
        alerts.append({
            "level": "warn",
            "category": "claude_md",
            "title": "Global CLAUDE.md not found",
            "detail": "No global instructions file at ~/.claude/CLAUDE.md",
            "action": "Create ~/.claude/CLAUDE.md",
        })
    else:
        line_count = _count_lines(global_md)
        if line_count > 200:
            alerts.append({
                "level": "warn",
                "category": "claude_md",
                "title": f"Global CLAUDE.md is {line_count} lines",
                "detail": "Recommended to keep under 200 lines for optimal performance",
                "action": "Review and trim CLAUDE.md",
            })
        else:
            alerts.append({
                "level": "ok",
                "category": "claude_md",
                "title": f"Global CLAUDE.md healthy ({line_count} lines)",
                "detail": "",
                "action": "",
            })

    # 1b. Per-project CLAUDE.md
    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.is_dir():
        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            decoded_path = decode_project_path(proj_dir.name)
            project_name = Path(decoded_path).name or proj_dir.name

            config_md = proj_dir / "CLAUDE.md"
            local_md = Path(decoded_path) / "CLAUDE.md"

            config_exists = config_md.is_file()
            local_exists = local_md.is_file()

            if config_exists:
                lc = _count_lines(config_md)
                if lc > 200:
                    alerts.append({
                        "level": "warn",
                        "category": "claude_md",
                        "title": f"Project \"{project_name}\" config CLAUDE.md is {lc} lines",
                        "detail": f"~/.claude/projects/.../{project_name}/CLAUDE.md — recommended under 200",
                        "action": "Review and trim project CLAUDE.md",
                    })

            if local_exists:
                lc = _count_lines(local_md)
                if lc > 200:
                    alerts.append({
                        "level": "warn",
                        "category": "claude_md",
                        "title": f"Project \"{project_name}\" local CLAUDE.md is {lc} lines",
                        "detail": f"{decoded_path}/CLAUDE.md — recommended under 200",
                        "action": "Review and trim project CLAUDE.md",
                    })

            if not config_exists and not local_exists:
                alerts.append({
                    "level": "warn",
                    "category": "claude_md",
                    "title": f"Project \"{project_name}\" has no CLAUDE.md",
                    "detail": f"Neither config nor local CLAUDE.md found for {decoded_path}",
                    "action": f"Create CLAUDE.md in {decoded_path}",
                })

    # ── 2. 활성 세션 컨텍스트 체크 (실제 토큰 데이터 기반) ──────────
    # 활성 세션 ID 수집
    active_sessions_data = get_sessions().get("sessions", [])
    active_pids = {s["pid"] for s in active_sessions_data if s.get("state") != "stopped"}
    active_session_ids = set()
    sessions_dir = CLAUDE_DIR / "sessions"
    if sessions_dir.is_dir():
        for sf in sessions_dir.glob("*.json"):
            try:
                sd = json.loads(read_text(sf) or "{}")
                pid = sd.get("pid")
                sid = sd.get("sessionId")
                if pid in active_pids and sid:
                    active_session_ids.add(sid)
            except Exception:
                continue

    # 활성 세션의 JSONL 파일에서 실제 토큰 사용량 추출
    session_ok_count = 0
    if projects_dir.is_dir():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl_file in proj_dir.glob("*.jsonl"):
                if "subagent" in str(jsonl_file):
                    continue
                session_id = jsonl_file.stem
                if session_id not in active_session_ids:
                    continue

                # slug/customTitle 추출
                last_5 = _read_last_n_lines(jsonl_file, 5)
                slug = ""
                custom_title = ""
                for line in reversed(last_5):
                    try:
                        entry = json.loads(line)
                        if not slug and entry.get("slug"):
                            slug = entry["slug"]
                    except Exception:
                        continue
                if not custom_title:
                    try:
                        result = subprocess.run(
                            ["grep", "-o", '"customTitle":"[^"]*"', str(jsonl_file)],
                            capture_output=True, text=True, timeout=3
                        )
                        if result.stdout.strip():
                            last_match = result.stdout.strip().splitlines()[-1]
                            custom_title = last_match.split(':"')[1].rstrip('"')
                    except Exception:
                        pass
                slug = custom_title or slug
                project_path = decode_project_path(proj_dir.name)
                project_name = Path(project_path).name or proj_dir.name
                display_name = (slug or session_id[:16]) + f" ({project_name})"

                # 마지막 20줄에서 실제 토큰 사용량 추출
                last_20 = _read_last_n_lines(jsonl_file, 20)
                token_data = _extract_token_usage(last_20)
                context_tokens = token_data.get("context_tokens", 0)
                context_pct = round(context_tokens / 1_000_000 * 100) if context_tokens > 0 else 0

                if context_tokens > 800_000:  # >80%: CRITICAL
                    alerts.append({
                        "level": "critical",
                        "category": "context",
                        "title": f"Session \"{display_name}\" context nearly full ({context_pct}%)",
                        "detail": f"{_format_token_display(context_tokens)} / 1M tokens used. Use /compact or /handoff immediately",
                        "action": "/compact or /handoff",
                    })
                elif context_tokens > 600_000:  # >60%: WARN
                    alerts.append({
                        "level": "warn",
                        "category": "context",
                        "title": f"Session \"{display_name}\" context getting large ({context_pct}%)",
                        "detail": f"{_format_token_display(context_tokens)} / 1M tokens used. Consider /compact soon",
                        "action": "/compact",
                    })
                else:
                    session_ok_count += 1

    if session_ok_count > 0:
        alerts.append({
            "level": "ok",
            "category": "context",
            "title": f"Session context healthy ({session_ok_count} session{'s' if session_ok_count != 1 else ''})",
            "detail": "",
            "action": "",
        })

    if len(active_session_ids) == 0:
        alerts.append({
            "level": "info",
            "category": "context",
            "title": "No active sessions detected",
            "detail": "Start a Claude session to see context monitoring",
            "action": "",
        })

    # ── 요약 계산 ──────────────────────────────────────────────────────
    summary = {"critical": 0, "warn": 0, "info": 0, "ok": 0}
    for a in alerts:
        level = a.get("level", "info")
        if level in summary:
            summary[level] += 1

    # critical/warn 먼저, ok 마지막 정렬
    level_order = {"critical": 0, "warn": 1, "info": 2, "ok": 3}
    alerts.sort(key=lambda a: level_order.get(a.get("level", "info"), 2))

    return {"alerts": alerts, "summary": summary}


# ─── HTTP 핸들러 ─────────────────────────────────────────────────────────────

# API 라우터: path → handler 매핑
API_ROUTES: dict[str, callable] = {
    "/api/health": get_health,
    "/api/sessions": get_sessions,
    "/api/activity": get_activity,
    "/api/projects-summary": get_projects_summary,
    "/api/instructions": get_instructions,
    "/api/skills": get_skills,
    "/api/agents": get_agents,
    "/api/connectors": get_connectors,
    "/api/hooks": get_hooks,
    "/api/plugins": get_plugins,
    "/api/forks": get_forks,
    "/api/project-status": get_project_status,
    "/api/session-detail": get_session_detail,
    "/api/alerts": get_alerts,
}


class CockpitHandler(BaseHTTPRequestHandler):
    """Claude Cockpit HTTP 요청 핸들러."""

    # 로그 출력 간소화
    def log_message(self, format, *args):
        # 간단한 한 줄 로그
        print(f"[{self.log_date_time_string()}] {args[0] if args else ''}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query_params = parse_qs(parsed.query)

        # API 엔드포인트: session-search (쿼리 파라미터 필요)
        if path == "/api/session-search":
            q = query_params.get("q", [""])[0]
            self._json_response(get_session_search(q))
            return

        # API 엔드포인트: session-xray (쿼리 파라미터 필요)
        if path == "/api/session-xray":
            sid = query_params.get("id", [""])[0]
            self._json_response(get_session_xray(sid))
            return

        # API 엔드포인트
        if path in API_ROUTES:
            self._json_response(API_ROUTES[path]())
            return

        # SSE 스트림
        if path == "/sse/live":
            self._sse_stream()
            return

        # 정적 파일 서빙
        self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query_params = parse_qs(parsed.query)

        if path == "/api/delete-project":
            dir_name = query_params.get("dir", [""])[0]
            if not dir_name:
                self._json_response({"error": "dir parameter required"}, 400)
                return
            # 안전 검증: projects 디렉토리 안의 폴더만 삭제 가능
            target = CLAUDE_DIR / "projects" / dir_name
            if not target.is_dir() or ".." in dir_name:
                self._json_response({"error": "invalid project directory"}, 400)
                return
            import shutil
            try:
                shutil.rmtree(target)
                self._json_response({"ok": True, "deleted": dir_name})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path == "/api/delete-skill":
            import shutil
            skill_name = query_params.get("name", [""])[0]
            if not skill_name or ".." in skill_name:
                self._json_response({"error": "name parameter required"}, 400)
                return
            # User skills only: ~/.claude/skills/{name}/
            target = CLAUDE_DIR / "skills" / skill_name
            plugin_cache = CLAUDE_DIR / "plugins" / "cache"
            # Verify target is NOT inside plugin cache
            try:
                target.resolve().relative_to(plugin_cache.resolve())
                self._json_response({"error": "cannot delete plugin skills"}, 403)
                return
            except ValueError:
                pass  # Not in plugin cache — OK
            if not target.is_dir():
                self._json_response({"error": "skill not found"}, 404)
                return
            try:
                shutil.rmtree(target)
                self._json_response({"ok": True})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path == "/api/delete-agent":
            agent_name = query_params.get("name", [""])[0]
            if not agent_name or ".." in agent_name:
                self._json_response({"error": "name parameter required"}, 400)
                return
            # User agents only: ~/.claude/agents/{name}.md
            target = CLAUDE_DIR / "agents" / (agent_name + ".md")
            plugin_cache = CLAUDE_DIR / "plugins" / "cache"
            # Verify target is NOT inside plugin cache
            try:
                target.resolve().relative_to(plugin_cache.resolve())
                self._json_response({"error": "cannot delete plugin agents"}, 403)
                return
            except ValueError:
                pass  # Not in plugin cache — OK
            if not target.is_file():
                self._json_response({"error": "agent not found"}, 404)
                return
            try:
                target.unlink()
                self._json_response({"ok": True})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path == "/api/delete-hook":
            event = query_params.get("event", [""])[0]
            index = query_params.get("index", ["0"])[0]
            if not event:
                self._json_response({"error": "event parameter required"}, 400)
                return
            try:
                idx = int(index)
            except ValueError:
                self._json_response({"error": "invalid index"}, 400)
                return
            settings_path = CLAUDE_DIR / "settings.json"
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                hooks = settings.get("hooks", {})
                if event not in hooks:
                    self._json_response({"error": f"event {event} not found"}, 404)
                    return
                handlers = hooks[event]
                if not isinstance(handlers, list) or idx >= len(handlers):
                    self._json_response({"error": "invalid index"}, 400)
                    return
                handlers.pop(idx)
                if not handlers:
                    del hooks[event]
                if not hooks:
                    del settings["hooks"]
                settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
                self._json_response({"ok": True})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path in ("/api/delete-session", "/api/delete-fork"):
            import shutil
            session_id = query_params.get("id", [""])[0]
            if not session_id or ".." in session_id or "/" in session_id:
                self._json_response({"error": "id parameter required"}, 400)
                return
            # Check against active sessions
            active_session_ids = set()
            sessions_dir = CLAUDE_DIR / "sessions"
            if sessions_dir.is_dir():
                running_pids = set()
                try:
                    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5,
                                        env={**os.environ, "LC_ALL": "C"})
                    for line in ps.stdout.splitlines()[1:]:
                        parts = line.split(None, 10)
                        if len(parts) >= 11 and "claude" in parts[10].lower():
                            try:
                                running_pids.add(int(parts[1]))
                            except ValueError:
                                pass
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
                for sf in sessions_dir.glob("*.json"):
                    try:
                        pid = int(sf.stem)
                        if pid in running_pids:
                            sess_data = read_json(sf)
                            if sess_data:
                                sid = sess_data.get("sessionId", "")
                                if sid:
                                    active_session_ids.add(sid)
                    except ValueError:
                        pass
            if session_id in active_session_ids:
                self._json_response({"error": "cannot delete active session"}, 400)
                return
            deleted_anything = False
            errors = []
            # Delete JSONL file(s) across all project dirs
            projects_dir = CLAUDE_DIR / "projects"
            if projects_dir.is_dir():
                for jsonl_file in projects_dir.rglob(f"{session_id}.jsonl"):
                    try:
                        jsonl_file.unlink()
                        deleted_anything = True
                    except Exception as e:
                        errors.append(str(e))
                # Delete subagent directory if exists
                for subdir in projects_dir.rglob(session_id):
                    if subdir.is_dir():
                        try:
                            shutil.rmtree(subdir)
                            deleted_anything = True
                        except Exception as e:
                            errors.append(str(e))
            # Remove sessions/*.json where sessionId matches
            if sessions_dir.is_dir():
                for sf in sessions_dir.glob("*.json"):
                    try:
                        sess_data = read_json(sf)
                        if sess_data and sess_data.get("sessionId", "") == session_id:
                            sf.unlink()
                            deleted_anything = True
                    except Exception as e:
                        errors.append(str(e))
            if errors:
                self._json_response({"error": "; ".join(errors)}, 500)
                return
            self._json_response({"ok": True})
            return

        self._json_response({"error": "not found"}, 404)

    def _json_response(self, data: Any, status: int = 200):
        """JSON 응답 전송."""
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _sse_stream(self):
        """Server-Sent Events 스트림. 5초마다 sessions/activity, 30초마다 alerts 전송."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        alerts_counter = 0  # alerts는 30초마다 (6 * 5초)

        try:
            while True:
                # sessions 이벤트
                sessions_data = get_sessions()
                event = {
                    "type": "sessions",
                    "data": sessions_data,
                }
                self._send_sse_event(event)

                # activity 이벤트
                activity_data = get_activity()
                event = {
                    "type": "activity",
                    "data": activity_data,
                }
                self._send_sse_event(event)

                # alerts 이벤트 (30초마다)
                if alerts_counter % 6 == 0:
                    alerts_data = get_alerts()
                    event = {
                        "type": "alerts",
                        "data": alerts_data,
                    }
                    self._send_sse_event(event)
                alerts_counter += 1

                self.wfile.flush()
                time.sleep(SSE_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 클라이언트 연결 끊김
            pass

    def _send_sse_event(self, data: dict):
        """단일 SSE 이벤트 전송."""
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))

    def _serve_static(self, path: str):
        """정적 파일 서빙 (dist/ 디렉토리)."""
        # / → /dist/index.html
        if path == "/":
            file_path = DIST_DIR / "index.html"
        elif path.startswith("/dist/"):
            file_path = DIST_DIR / path[len("/dist/"):]
        elif path.startswith("/assets/"):
            file_path = DIST_DIR / path[1:]  # assets/ 그대로
        else:
            # dist 내에서 찾기
            file_path = DIST_DIR / path.lstrip("/")

        # 보안: 경로 탈출 방지
        try:
            file_path = file_path.resolve()
            dist_resolved = DIST_DIR.resolve()
            if not str(file_path).startswith(str(dist_resolved)):
                self._error_response(403, "Forbidden")
                return
        except (ValueError, OSError):
            self._error_response(400, "Bad Request")
            return

        if not file_path.is_file():
            # SPA 라우팅: 파일이 없으면 index.html 반환
            index = DIST_DIR / "index.html"
            if index.is_file() and not path.startswith("/api/"):
                file_path = index
            else:
                self._error_response(404, "Not Found")
                return

        # MIME 타입 결정
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".json": "application/json; charset=utf-8",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
        }
        suffix = file_path.suffix.lower()
        content_type = mime_map.get(suffix, mime_type or "application/octet-stream")

        try:
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        except (FileNotFoundError, PermissionError):
            self._error_response(500, "Internal Server Error")

    def _error_response(self, status: int, message: str):
        """에러 JSON 응답."""
        self._json_response({"error": message}, status=status)


# ─── 서버 실행 ───────────────────────────────────────────────────────────────

class ThreadedHTTPServer(HTTPServer):
    """요청마다 스레드를 생성하는 HTTP 서버 (SSE 지원을 위해 필수)."""
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        """각 요청을 별도 스레드에서 처리."""
        t = threading.Thread(target=self._handle_request_thread,
                             args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    server = ThreadedHTTPServer((BIND_HOST, BIND_PORT), CockpitHandler)
    print(f"Serving on http://{BIND_HOST}:{BIND_PORT}")
    print(f"→ Internal network: http://{INTERNAL_IP}:{BIND_PORT}")
    print(f"→ Static files: {DIST_DIR}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버 종료...")
        server.shutdown()


if __name__ == "__main__":
    main()
