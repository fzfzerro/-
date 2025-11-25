#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
400万字网文AI创作系统 · 自动化 Copilot Pipeline (Optimized V2)
===============================================================

本脚本用于自动化执行 Stage TODO 清单，通过 GitHub Issues + Copilot 完成整个创作流程。

核心功能：
1. 扫描 todo/Stage-*.todos.md 文件，提取未勾选的 TODO
2. 为每个 TODO 或 TODO 批次创建 GitHub Issue（包含完整的执行指令）
3. 通过 Issue Assignment 触发 GitHub Copilot 自动执行
4. 监控 Copilot 创建的 PR 状态，等待 copilot_work_finished 信号
5. 自动批准并合并 PR，关闭 Issue
6. 循环执行直到所有 TODO 完成

关键修复：
- 使用正确的 Copilot 触发机制：Issue Assignment（而非评论）
- 将所有任务详情和执行指令放入 Issue Body（Copilot 只读取初始描述）
- 使用正确的 bot 名称 "Copilot"（gh CLI 可识别）
- 移除无效的评论触发逻辑

"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional
from urllib.parse import urlparse

# ==================== 项目配置 ====================

ROOT = Path(__file__).resolve().parents[1]
TODO_ROOT = ROOT / "todo"


def _extract_owner_repo(path: str) -> Optional[tuple[str, str]]:
    parts = [segment for segment in path.strip("/").split("/") if segment]
    if len(parts) >= 2:
        owner, repo = parts[-2], parts[-1]
        return owner, repo
    return None


def detect_repo_from_git() -> Optional[tuple[str, str]]:
    """尝试从 git remote 中解析 owner/repo，兼容多种 URL 格式"""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8"
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    url = result.stdout.strip()
    if not url:
        return None

    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    if url.startswith("git@"):
        path = url.split(":", 1)[1]
        return _extract_owner_repo(path)

    parsed = urlparse(url)
    if parsed.scheme in {"https", "http", "ssh", "git"}:
        return _extract_owner_repo(parsed.path)

    # 兼容类似 github.com/owner/repo 或 file 协议的路径
    if ":" not in url and "/" in url:
        return _extract_owner_repo(url)

    return None


def resolve_repo() -> tuple[str, str]:
    detected = detect_repo_from_git()
    if not detected:
        raise RuntimeError(
            "无法通过 git remote.origin.url 自动检测仓库信息，请在仓库中配置 remote.origin 后再运行。"
        )
    return detected

# GitHub Copilot bot 的名称配置
# 不同系统和 gh CLI 版本可能使用不同的名称格式
COPILOT_ASSIGNEES = ["@copilot", "copilot", "Copilot", "github-copilot", "github-copilot[bot]"]  # 按优先级尝试
COPILOT_USERNAME = "copilot"

# 轮询配置
DEFAULT_POLL_INTERVAL = 60  # 秒
DEFAULT_MAX_WAIT = 36000  # 单个 Issue 最大等待时间：10小时
DEFAULT_BATCH_SIZE = 1  # 每个 Issue 包含的 TODO 数量（原子执行）
DEFAULT_TASK_MAX_RETRIES = 3  # 任务失败重试次数
DEFAULT_TASK_RETRY_WAIT = 300  # 任务重试等待时间：5分钟
DEFAULT_MAX_PR_RESETS = 3  # 单个 Issue 内最大 PR 重置次数
PR_TIMEOUT = 10800  # PR 处理超时：3小时
PR_WAIT_TIMEOUT = 1800  # 等待 PR 创建超时：30分钟
RESET_WAIT_TIME = 30  # 重置后的等待时间（秒）
HEARTBEAT_INTERVAL = 300  # 长时间等待时的心跳日志间隔（秒）
GH_TIMEOUT = 180  # GitHub CLI 命令超时（秒），从 120 增加到 180
RETRY_SLEEP_SHORT = 5  # 短暂重试等待（秒）
NETWORK_ERROR_BASE_WAIT = 10  # 网络错误基础等待时间（秒）
NETWORK_ERROR_MAX_WAIT = 120  # 网络错误最大等待时间（秒）
PR_READY_WAIT = 2  # PR 标记 Ready 后等待时间（秒）
MAIN_ERROR_WAIT = 30  # 主循环错误重试等待（秒）
TIMELINE_ACCEPT_HEADER = "Accept: application/vnd.github.mockingbird-preview+json"

CORE_DOCUMENTS = {
    ROOT / "Project-Bible.md": "# Project Bible\n\n> 本文件由 auto_copilot_pipeline.py 自动创建，用于维护世界观、角色与伏笔总账。\n\n",
    ROOT / "Risk-Ledger.md": "# Risk Ledger\n\n> 本文件由 auto_copilot_pipeline.py 自动创建，用于记录风险、决策与后续动作。\n\n"
}

# ==================== 正则表达式 ====================

STAGE_FILE_PATTERN = re.compile(r"^Stage-(\d+)_(.+)\.todos\.md$")
# 修正：TODO 行实际是 ### 开头（三级标题 + 列表项）
# 使用非贪婪匹配避免 title 中包含 ] 导致解析失败
TODO_LINE_PATTERN = re.compile(
    r"^###\s+-\s*\[(?P<status>[ xX])\]\s+\[(?P<todo_id>[^\]]+?)\]\s+(?P<title>.+)$"
)

# ==================== 日志配置 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("copilot-pipeline")


def ensure_core_documents() -> None:
    created: List[str] = []
    for path, placeholder in CORE_DOCUMENTS.items():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(placeholder, encoding="utf-8")
            created.append(path.name)
    if created:
        logger.info("自动创建关键文档: %s", ", ".join(created))

# ==================== 数据模型 ====================

@dataclass
class TodoItem:
    id_full: str
    stage_number: int
    title: str
    meta_lines: List[str]
    file_path: Path

    @property
    def stage_code(self) -> str:
        return f"{self.stage_number:02d}"

@dataclass
class WorkItem:
    id_full: str
    stage_number: int
    title: str
    file_path: Path
    todos: List[TodoItem]
    batch_index: Optional[int] = None
    batch_total: Optional[int] = None

    @property
    def stage_code(self) -> str:
        return f"{self.stage_number:02d}"

    @property
    def is_batch(self) -> bool:
        return len(self.todos) > 1

# ==================== Issue 模板 ====================

ISSUE_BODY_TEMPLATE = """## 📋 任务概览

{task_overview}

---

## 🎯 执行要求

请严格按照 `.github/copilot-instructions.md` 中的**统一自动化流水线 (The Unified Loop)** 执行：

### 1. 寻标 (Scan)
- 📁 读取 `{stage_file}`
- 🔍 定位到指定的 TODO 项{plural}
- 📖 读取下方的任务详情和元信息

### 2. 专家议会 (Council & Think)
- 👥 组建 3 人专家小组（主理人 + 商业顾问 + 风控）
- 🔎 检索相关文件（Project-Bible.md 等）
- ⚠️ 冲突检查：新构思是否违背旧设定？
- 💰 价值评估：符合北极星指标吗？
- 🔬 深度挖掘：挖掘所有可能的分支和细节

### 3. 规划 (Plan)
- 📂 确定输出路径：`archives/Stage-{stage_code}_*/{{Filename}}.md`
- 📋 确定依赖文件
- 🔄 确定是否需要更新 Project-Bible.md

### 4. 生产 (Draft)
- ✍️ 输出**详尽完整**的内容，严禁省略
- 🚫 执行去 AI 味协议（禁用：然而、显然、就在这时等）
- ✅ 使用 Show-Don't-Tell，强制短句
- 🎣 每 2000 字检查商业钩子

### 5. 质检 (Verify)
- 🔍 自我审视：满足验收标准吗？
- 🤔 像人类大神写的吗？
- 🔁 不满意立即 #redo，不要问用户

### 6. 归档 (Commit)
- 💾 保存文件到指定路径
- 📝 更新 `Project-Bible.md`（如有新设定/伏笔/角色）
- ⚠️ 更新 `Risk-Ledger.md`（如有未决问题）
- ✅ 勾选 TODO：将 `- [ ]` 改为 `- [x]`

---

## ⚠️ 绝对硬约束

1. **原子化执行**：每个 TODO 独立完成，严禁批量勾选
2. **闭环交付**：必须有实质性产出，禁止"略"、"待补充"
3. **深度思考**：拒绝敷衍，最大化 AI 算力
4. **强制中文**：所有输出使用简体中文

---

## 📦 交付标准

- [ ] TODO 已勾选（修改 `{stage_file}`）
- [ ] 产出已归档（保存到 `archives/` 目录）
- [ ] 设定已更新（同步到 Project-Bible.md）
- [ ] PR 包含自检清单
- [ ] PR 描述包含 `Fixes #{{issue_number}}`

---

## 📚 参考文件

{reference_files}

---

## 💡 提示

- 如遇到非阻断性问题，依据设定库自行决策并记录日志
- 每个 TODO 都是独立的创作行为，需要完整的六步循环
- 不要吝啬 Token，在 #think 和 #draft 阶段尽可能详尽
"""

# ==================== 辅助函数 ====================

def extract_stage_number_from_filename(path: Path) -> int:
    match = STAGE_FILE_PATTERN.match(path.name)
    if not match:
        raise ValueError(f"无法解析 Stage 编号: {path.name}")
    return int(match.group(1))

def stage_file_sort_key(path: Path) -> tuple[int, str]:
    return extract_stage_number_from_filename(path), path.name

# ==================== GitHub 客户端 ====================

class GitHubClient:
    def __init__(self, owner: str, repo: str) -> None:
        self.owner = owner
        self.repo = repo
        self.repo_ref = f"{owner}/{repo}"
        if not shutil.which("gh"):
            raise RuntimeError("未找到 gh CLI")

    def _run_gh(self, args: List[str], retries: int = 3) -> str:
        cmd = ["gh"] + args
        for attempt in range(1, retries + 1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=True,
                    encoding="utf-8", timeout=GH_TIMEOUT
                )
                return result.stdout.strip()
            except subprocess.TimeoutExpired:
                if attempt == retries:
                    logger.error(f"gh 命令最终超时失败（{retries} 次尝试）: {' '.join(args[:3])}...")
                    raise RuntimeError(f"gh 命令超时: {' '.join(args)}")
                wait_time = min(2 ** attempt * NETWORK_ERROR_BASE_WAIT, NETWORK_ERROR_MAX_WAIT)
                logger.warning(f"gh 命令超时，{wait_time}秒后重试 ({attempt}/{retries}): {' '.join(args[:3])}...")
                time.sleep(wait_time)
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr.strip() if exc.stderr else "无错误信息"

                # 检测网络相关错误
                NETWORK_ERRORS = {"tls handshake", "bad gateway", "connection reset",
                                 "connection refused", "network", "timeout", "eof",
                                 "502", "503", "504"}
                is_network_error = any(keyword in stderr.lower() for keyword in NETWORK_ERRORS)

                # 最后一次尝试，直接抛出异常
                if attempt == retries:
                    error_type = "网络错误" if is_network_error else "命令错误"
                    logger.error(f"gh {error_type}最终失败（{retries} 次尝试）: {stderr[:200]}")
                    raise RuntimeError(f"gh 命令失败: {stderr}") from exc

                # 某些错误不值得重试（立即失败）
                if "not found" in stderr.lower() or "unknown" in stderr.lower():
                    logger.error(f"gh 命令致命错误（不可重试）: {stderr}")
                    raise RuntimeError(f"gh 命令错误: {stderr}") from exc

                # 针对 Rate Limit 的特殊处理
                if "rate limit" in stderr.lower() or "abuse" in stderr.lower():
                    wait_time = min(300 * attempt, 1800)  # 最多等待 30 分钟
                    logger.warning(f"GitHub API 限流警告，暂停 {wait_time}秒 ({wait_time/60:.1f}min) 后重试 ({attempt}/{retries})")
                    time.sleep(wait_time)
                # 网络错误使用指数退避
                elif is_network_error:
                    wait_time = min(2 ** attempt * NETWORK_ERROR_BASE_WAIT, NETWORK_ERROR_MAX_WAIT)
                    logger.warning(f"网络错误，{wait_time}秒后重试 ({attempt}/{retries}): {stderr[:100]}")
                    time.sleep(wait_time)
                else:
                    wait_time = min(2 ** attempt, 30)
                    logger.warning(f"gh 命令失败，{wait_time}秒后重试 ({attempt}/{retries}): {stderr[:100]}")
                    time.sleep(wait_time)

        raise RuntimeError(f"gh 命令失败：未知错误 (重试 {retries} 次后仍失败)")

    def api_request(self, method: str, path: str, headers: Optional[List[str]] = None,
                   silent_fail: bool = False) -> Any:
        """发起 GitHub API 请求

        Args:
            method: HTTP 方法
            path: API 路径
            headers: 可选的 HTTP 头
            silent_fail: 如果为 True，失败时返回空字典；否则抛出异常
        """
        if path.startswith("https://api.github.com"):
            path = path.replace("https://api.github.com", "")

        args = ["api", path, "--method", method]
        if headers:
            for header in headers:
                args.extend(["-H", header])

        try:
            output = self._run_gh(args)
            return json.loads(output) if output else {}
        except Exception as exc:
            if silent_fail:
                logger.debug(f"GitHub API 请求失败 ({method} {path}): {exc}")
                return {}
            else:
                logger.warning(f"GitHub API 请求失败 ({method} {path}): {exc}")
                raise

    def create_issue(self, title: str, body: str) -> int:
        args = ["issue", "create", "--repo", self.repo_ref, "--title", title, "--body", body]
        output = self._run_gh(args)
        try:
            # gh CLI 返回 Issue URL，提取最后的数字
            issue_num = int(output.strip().split('/')[-1])
            if issue_num <= 0:
                raise ValueError(f"无效的 Issue 编号: {issue_num}")
            return issue_num
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"解析 Issue 编号失败，输出: {output}") from e

    def add_assignees(self, issue_number: int, assignees: List[str]) -> None:
        """分配 Issue 给指定用户/Bot，尝试多个名称格式"""
        last_error = None
        for assignee in assignees:
            try:
                self._run_gh([
                    "issue", "edit", str(issue_number),
                    "--repo", self.repo_ref, "--add-assignee", assignee
                ])
                logger.debug(f"成功分配 Issue #{issue_number} 给 {assignee}")
                return  # 成功则立即返回
            except RuntimeError as e:
                last_error = e
                logger.debug(f"尝试分配给 {assignee} 失败: {e}")
                continue
            except Exception as e:
                last_error = e
                logger.debug(f"尝试分配给 {assignee} 时异常: {e}")
                continue

        # 所有名称都失败了，抛出最后一个错误
        if last_error:
            raise RuntimeError(f"无法分配 Issue #{issue_number} 给 Copilot（已尝试 {len(assignees)} 个名称）: {last_error}") from last_error

    def remove_assignees(self, issue_number: int, assignees: List[str]) -> None:
        """取消分配 Issue 的指定用户/Bot"""
        for assignee in assignees:
            try:
                self._run_gh([
                    "issue", "edit", str(issue_number),
                    "--repo", self.repo_ref, "--remove-assignee", assignee
                ])
                logger.debug(f"成功取消分配 Issue #{issue_number} 的 {assignee}")
            except Exception as e:
                # 取消分配失败不是致命错误，可能该用户本来就没分配
                logger.debug(f"取消分配 {assignee} 失败（可能未分配）: {e}")

    def comment_issue(self, issue_number: int, body: str) -> None:
        self._run_gh(["issue", "comment", str(issue_number), "--repo", self.repo_ref, "--body", body])

    def get_issue(self, issue_number: int) -> dict:
        """获取 Issue 信息，包含状态和分配者"""
        output = self._run_gh([
            "issue", "view", str(issue_number), "--repo", self.repo_ref,
            "--json", "number,state,assignees"
        ])
        try:
            return json.loads(output) if output else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"解析 Issue 数据失败: {e}") from e

    def get_pull(self, pr_number: int) -> dict:
        output = self._run_gh([
            "pr", "view", str(pr_number), "--repo", self.repo_ref,
            "--json", "number,state,mergedAt,isDraft,updatedAt,files"
        ])
        try:
            data = json.loads(output) if output else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"解析 PR 数据失败: {e}") from e

        # 统一字段名（Python 风格）
        if "mergedAt" in data:
            data["merged_at"] = data.pop("mergedAt")
        if "isDraft" in data:
            data["draft"] = data.pop("isDraft")
        return data

    def merge_pull(self, pr_number: int) -> dict:
        self._run_gh(["pr", "merge", str(pr_number), "--repo", self.repo_ref, "--squash", "--delete-branch"])
        return {"merged": True}

    def close_pr(self, pr_number: int, delete_branch: bool = True) -> None:
        """关闭 PR 并可选删除分支

        注意：关闭 PR（未合并）不会自动关闭关联的 Issue，
        即使 PR 描述中包含 'Fixes #123'。
        只有合并 PR 才会触发 Issue 的自动关闭。
        """
        args = ["pr", "close", str(pr_number), "--repo", self.repo_ref]
        if delete_branch:
            args.append("--delete-branch")
        self._run_gh(args)

    def list_closed_issues(self, limit: int = 1000) -> List[dict]:
        """查询已关闭的 Issues"""
        output = self._run_gh([
            "issue", "list", "--repo", self.repo_ref,
            "--state", "closed",
            "--limit", str(limit), "--json", "title"
        ])
        try:
            issues = json.loads(output) if output else []
            return issues if isinstance(issues, list) else []
        except json.JSONDecodeError:
            return []

    def find_issue_by_todo(self, todo_id: str) -> Optional[int]:
        """尝试根据标题中的 TODO ID 查找已有的开放 Issue"""
        if not todo_id or not todo_id.strip():
            return None

        try:
            output = self._run_gh([
                "issue", "list", "--repo", self.repo_ref,
                "--state", "open",
                "--search", f"[{todo_id}]",
                "--json", "number,title"
            ], retries=2)
        except Exception as e:
            logger.warning(f"查询现有 Issue 失败: {e}")
            return None

        try:
            issues = json.loads(output) if output else []
            if not isinstance(issues, list):
                logger.warning(f"Issue 查询返回了非列表数据: {type(issues)}")
                return None
        except json.JSONDecodeError as e:
            logger.warning(f"解析 Issue 列表失败: {e}")
            return None

        for issue in issues:
            if not isinstance(issue, dict):
                continue
            title = issue.get("title", "")
            issue_num = issue.get("number")
            if f"[{todo_id}]" in title and isinstance(issue_num, int):
                return issue_num
        return None

    def latest_pr_from_timeline(self, issue_number: int) -> Optional[int]:
        events = self.api_request(
            "GET",
            f"/repos/{self.repo_ref}/issues/{issue_number}/timeline",
            headers=[TIMELINE_ACCEPT_HEADER],
            silent_fail=True  # timeline 查询失败不应终止流程
        )
        if not isinstance(events, list): return None
        for event in reversed(events):
            if event.get("event") == "cross-referenced":
                source = event.get("source", {}).get("issue", {})
                if "pull_request" in source:
                    return source["number"]
        return None

    def mark_pr_ready(self, pr_number: int) -> None:
        """将 PR 标记为 Ready 状态

        注意：此方法会将 Draft PR 或 "Ready for Review" 状态的 PR 转为 Ready。
        如果 PR 已经是 Ready 状态，命令可能会失败，这是正常行为。
        """
        try:
            self._run_gh(["pr", "ready", str(pr_number), "--repo", self.repo_ref])
            logger.debug(f"成功调用 gh pr ready #{pr_number}")
        except Exception as e:
            error_msg = str(e).lower()
            # 如果错误信息表明 PR 已经 ready，这不是真正的错误
            if "already" in error_msg or "not a draft" in error_msg:
                logger.debug(f"PR #{pr_number} 已处于 Ready 状态")
            else:
                logger.warning(f"标记 PR #{pr_number} 为 Ready 失败: {e}")

# ==================== PR 监控 ====================

def check_copilot_signal(github: GitHubClient, pr_number: int) -> bool:
    """检查 PR 中是否有 copilot_work_finished 事件"""
    events = github.api_request(
        "GET",
        f"/repos/{github.repo_ref}/issues/{pr_number}/timeline",
        headers=[TIMELINE_ACCEPT_HEADER],
        silent_fail=True  # 信号检测失败时返回 False 而不是抛异常
    )

    if isinstance(events, list):
        for event in reversed(events):
            if event.get("event") == "copilot_work_finished":
                return True
    return False

# ==================== 解析器 ====================

def parse_stage_structure(path: Path) -> tuple[int, List[TodoItem]]:
    stage_num = extract_stage_number_from_filename(path)
    logger.info(f"解析 {path.name}")

    if not path.exists():
        logger.error(f"文件不存在: {path}")
        return stage_num, []

    if not path.is_file():
        logger.error(f"路径不是文件: {path}")
        return stage_num, []

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        logger.error(f"读取文件失败 {path}: {e}")
        return stage_num, []

    todos = []
    idx = 0
    total_lines = len(lines)

    while idx < total_lines:
        line = lines[idx]
        match = TODO_LINE_PATTERN.match(line)
        if not match:
            idx += 1
            continue

        # 跳过已完成的任务
        if match.group("status").lower() == "x":
            logger.debug(f"跳过已完成任务: {match.group('todo_id')}")
            # 跳过该任务的元信息块，直到下一个 TODO 或 Group Header
            idx += 1
            while idx < total_lines:
                next_line = lines[idx]
                if TODO_LINE_PATTERN.match(next_line) or (next_line.strip().startswith('##') and 'Group' in next_line):
                    break
                idx += 1
            # idx 现在指向下一个 TODO 或 Group，继续外层循环
            continue

        todo_id = match.group("todo_id").strip()
        title = match.group("title").strip()

        # 提取元信息：读取直到下一个 TODO 或 Group Header
        meta = []
        j = idx + 1
        while j < total_lines:
            meta_line = lines[j]
            # 检测下一个 TODO 或分组标题
            if TODO_LINE_PATTERN.match(meta_line) or (meta_line.strip().startswith('##') and 'Group' in meta_line):
                break
            meta.append(meta_line)
            j += 1

        # 清理尾部空行和分隔符
        while meta and (not meta[-1].strip() or meta[-1].strip() == '---'):
            meta.pop()

        # 如果 meta 为空，记录调试信息但继续
        if not meta:
            logger.debug(f"TODO {todo_id} 没有元信息")
        else:
            logger.debug(f"TODO {todo_id}: {len(meta)} 行元信息")

        todos.append(TodoItem(todo_id, stage_num, title, meta, path))
        idx = j

    logger.info(f"解析完成: {len(todos)} 个待办任务")
    return stage_num, todos

def iter_work_items(todo_root: Path, batch_size: int, completed_ids: set[str]) -> List[WorkItem]:
    """获取当前需要处理的工作项

    注意：采用 Stage 锁定机制，每次只返回一个 Stage 的任务。
    这确保了任务按 Stage 顺序执行，完成一个 Stage 后再处理下一个。

    Args:
        todo_root: TODO 文件所在目录
        batch_size: 每个 Issue 包含的 TODO 数量
        completed_ids: 已完成的 TODO ID 集合

    Returns:
        当前需要处理的工作项列表（只包含一个 Stage）
    """
    if not todo_root.exists():
        logger.warning(f"TODO 目录不存在: {todo_root}")
        return []

    if not todo_root.is_dir():
        logger.error(f"TODO 路径不是目录: {todo_root}")
        return []

    files = sorted(todo_root.glob("Stage-*.todos.md"), key=stage_file_sort_key)

    # 修复：确保 batch_size 至少为 1（原子执行原则）
    batch_size = max(1, batch_size)

    for path in files:
        stage_num, todos = parse_stage_structure(path)
        if not todos:
            continue

        batches = [todos[i:i + batch_size] for i in range(0, len(todos), batch_size)]

        stage_items: List[WorkItem] = []
        for i, batch in enumerate(batches, 1):
            if len(batch) == 1:
                stage_items.append(WorkItem(batch[0].id_full, stage_num, batch[0].title, path, batch))
            else:
                wid = f"S{stage_num:02d}-BATCH-{i:02d}"
                title = f"{path.stem} 批次 {i}/{len(batches)}"
                stage_items.append(WorkItem(wid, stage_num, title, path, batch, i, len(batches)))

        filtered_items: List[WorkItem] = []
        for item in stage_items:
            if item.id_full in completed_ids:
                logger.info(f"⏭ 跳过已完成任务/批次 (GitHub): {item.id_full}")
                continue

            if item.is_batch:
                all_sub_completed = all(todo.id_full in completed_ids for todo in item.todos)
                if all_sub_completed:
                    logger.info(f"⏭ 跳过已完成批次 (子任务全清): {item.id_full}")
                    continue

            filtered_items.append(item)

        if filtered_items:
            logger.info(f"锁定 Stage {stage_num:02d}，待处理 {len(filtered_items)} 个任务")
            return filtered_items

    return []

# ==================== Pipeline ====================

class Pipeline:
    def __init__(self, github: Optional[GitHubClient], args: argparse.Namespace) -> None:
        self.github = github
        self.args = args

    def _require_github(self) -> GitHubClient:
        if not self.github:
            raise RuntimeError("GitHub 客户端不可用，无法执行在线操作。请移除 --dry-run 或正确配置 gh CLI。")
        return self.github

    def get_recent_completed_todos(self, limit: int = 1000) -> set[str]:
        """获取最近已完成的 TODO ID 集合，用于自动跳过

        注意：此方法依赖 GitHub API，如果需要更快的启动速度，
        可以考虑将进度缓存到本地文件（如 .pipeline_progress.json）
        """
        if not self.github:
            logger.debug("跳过远程进度扫描：当前为 dry-run 或 GitHub 未配置")
            return set()

        github = self._require_github()

        try:
            # 查询所有已关闭的 Issues
            issues = github.list_closed_issues(limit)
            if not issues:
                logger.debug("未从 GitHub 获取到已关闭的 Issue")
                return set()

            completed = set()
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                title = issue.get("title", "")
                if not title:
                    continue
                match = re.search(r"\[([^\]]+?)\]", title)
                if match:
                    todo_id = match.group(1).strip()
                    if todo_id:
                        completed.add(todo_id)

            if completed:
                logger.info(f"从 GitHub 获取到 {len(completed)} 个最近完成的任务记录")
            else:
                logger.debug("未从 GitHub 获取到已完成的任务记录")
            return completed
        except json.JSONDecodeError as e:
            logger.warning(f"解析 Issue 列表失败: {e}")
            return set()
        except Exception as e:
            logger.warning(f"获取已完成任务失败: {e}")
            return set()

    def run(self, items: Iterable[WorkItem]) -> None:
        items_list = items if isinstance(items, list) else list(items)
        total = len(items_list)
        logger.info(f"\n{'='*80}")
        logger.info(f"工作队列统计")
        logger.info(f"{'='*80}")
        logger.info(f"待处理工作项总数: {total}")
        if total == 0:
            logger.info("✓ 所有 TODO 已完成，无需进一步操作。")
            return

        # 按 Stage 分组统计
        stage_counts = {}
        for item in items_list:
            stage_counts[item.stage_number] = stage_counts.get(item.stage_number, 0) + 1
        for stage, count in sorted(stage_counts.items()):
            logger.info(f"  Stage {stage:02d}: {count} 个任务")
        logger.info(f"{'='*80}\n")

        max_task_retries = max(1, self.args.task_max_retries)
        base_retry_wait = max(1, self.args.task_retry_wait)

        failed_items = []  # 记录失败的任务

        for i, item in enumerate(items_list, 1):
            logger.info(f"\n{'='*80}")
            logger.info(f"📋 进度: {i}/{total} ({i*100//total}%)")
            logger.info(f"🔖 工作项: {item.id_full}")
            logger.info(f"📝 标题: {item.title}")
            if item.is_batch:
                logger.info(f"📦 批次: {item.batch_index}/{item.batch_total} (包含 {len(item.todos)} 个子任务)")
            logger.info(f"{'='*80}")

            # 任务级重试机制
            for attempt in range(1, max_task_retries + 1):
                try:
                    issue_num = self._ensure_issue(item)
                    if not self.args.dry_run:
                        self._wait_and_merge(item, issue_num)
                    logger.info(f"\n✓ [{i}/{total}] {item.id_full} 完成\n")
                    break  # 成功则跳出重试循环
                except Exception as e:
                    logger.error(f"\n✗ [{i}/{total}] {item.id_full} 失败 (尝试 {attempt}/{max_task_retries}): {e}")
                    if attempt < max_task_retries:
                        wait_time = base_retry_wait * (2 ** (attempt - 1))  # 指数退避: base, 2*base, 4*base, ...
                        logger.warning(f"等待 {wait_time} 秒后重试...")
                        time.sleep(wait_time)
                    else:
                        # 重试耗尽，记录失败但继续处理后续任务
                        logger.error(f"✗✗✗ [{i}/{total}] {item.id_full} 最终失败，跳过并继续后续任务")
                        logger.exception("详细错误信息：")
                        failed_items.append((item.id_full, str(e)))
                        break

        # 所有任务处理完后，报告失败的任务
        if failed_items:
            logger.error(f"\n{'='*80}")
            logger.error(f"⚠️  有 {len(failed_items)} 个任务最终失败：")
            for task_id, error in failed_items:
                logger.error(f"  - {task_id}: {error}")
            logger.error(f"{'='*80}\n")
            logger.error("请手动处理失败的任务，然后重新运行脚本")

    def _ensure_issue(self, item: WorkItem) -> int:
        if self.args.dry_run:
            logger.info(f"[DRY RUN] 创建 Issue: {item.title}")
            return 0

        github = self._require_github()
        existing = github.find_issue_by_todo(item.id_full)

        # 尝试复用已有的 open Issue
        if existing:
            logger.info(f"检测到线上已有 Issue #{existing}")
            try:
                issue_data = github.get_issue(existing)
                state = issue_data.get('state')
                logger.debug(f"Issue #{existing} 状态: {state}")

                if state == "open":
                    # Issue 是 open 状态，确保 Copilot 已分配
                    assignees = {
                        (assignee.get("login") or "").lower()
                        for assignee in issue_data.get("assignees", []) if assignee
                    }
                    if COPILOT_USERNAME not in assignees:
                        github.add_assignees(existing, COPILOT_ASSIGNEES)
                        logger.info(f"✓ 已将 Issue #{existing} 重新分配给 Copilot")
                    else:
                        logger.debug(f"Issue #{existing} 已分配给 Copilot，直接复用")
                    return existing
                else:
                    # Issue 已关闭或未知状态，创建新的
                    logger.warning(f"Issue #{existing} 状态为 {state}，将创建新 Issue")
            except Exception as e:
                logger.warning(f"获取 Issue #{existing} 详情失败: {e}，将创建新 Issue")

        # 构建 Issue Body
        body = self._build_body(item)
        # 构建完整的 Issue Body（包含所有任务详情和执行指令）
        full_body = self._build_full_issue_body(item, body)

        issue_num = github.create_issue(f"[{item.id_full}] {item.title}", full_body)
        logger.info(f"创建 Issue #{issue_num}")

        # 关键：通过 Assignment 触发 Copilot（而不是评论）
        logger.info(f"分配 Issue #{issue_num} 给 Copilot 以触发自动执行...")
        try:
            github.add_assignees(issue_num, COPILOT_ASSIGNEES)
            logger.info(f"✓ 成功分配给 Copilot")
        except Exception as e:
            # 分配失败是严重错误，必须抛出异常
            logger.error(f"✗ 分配 Issue #{issue_num} 给 Copilot 失败: {e}")
            logger.error("分配失败意味着 Copilot 不会被触发，将抛出异常以触发重试")
            raise RuntimeError(f"无法分配 Issue #{issue_num} 给 Copilot，请检查权限配置: {e}") from e

        return issue_num

    def _build_body(self, item: WorkItem) -> str:
        """构建任务详情部分（TODO 列表）"""
        lines = [
            "### 📋 任务详情",
            ""
        ]
        for todo in item.todos:
            lines.append(f"#### {todo.id_full} {todo.title}")
            lines.extend(todo.meta_lines)
            lines.append("")
        return "\n".join(lines)

    def _build_full_issue_body(self, item: WorkItem, task_details: str) -> str:
        """构建完整的 Issue Body，包含执行指令和任务详情"""
        try:
            relative_path = item.file_path.relative_to(ROOT).as_posix()
        except ValueError:
            # 如果路径不在 ROOT 下，使用绝对路径
            relative_path = item.file_path.as_posix()

        reference_files = f"""- `.github/copilot-instructions.md`
- `{relative_path}`
- `Stages/Stage-{item.stage_code}_*.md`
- `Project-Bible.md` (如不存在将自动创建)
- `Risk-Ledger.md` (如不存在将自动创建)"""

        instruction_body = ISSUE_BODY_TEMPLATE.format(
            task_overview=f"- **文件**: `{relative_path}`\n- **任务ID**: `{item.id_full}`\n- **TODO数量**: {len(item.todos)}",
            stage_file=relative_path,
            stage_code=item.stage_code,
            plural="s" if item.is_batch else "",
            reference_files=reference_files
        )

        return f"{instruction_body}\n\n---\n\n{task_details}"

    def _wait_and_merge(self, item: WorkItem, issue_num: int) -> None:
        if self.args.dry_run:
            return

        github = self._require_github()
        reset_count = 0

        issue_start_time = time.time()
        wait_start_time = issue_start_time
        pr_create_time = None
        current_pr = None
        last_heartbeat = issue_start_time

        logger.info(f"开始监控 Issue #{issue_num}，PR 超时 {PR_TIMEOUT/3600:.1f}h，最大重置 {DEFAULT_MAX_PR_RESETS} 次")

        while True:
            elapsed_total = time.time() - issue_start_time

            # 检查总超时：防止 Issue 卡死无限等待
            if elapsed_total >= self.args.issue_max_wait:
                raise RuntimeError(f"Issue #{issue_num} 总超时 ({self.args.issue_max_wait/3600:.1f}h)")

            # 检查 Issue 是否已关闭
            try:
                issue_data = github.get_issue(issue_num)
                issue_state = issue_data.get("state")
                if issue_state == "closed":
                    # 警告：Issue 被关闭但可能 PR 未合并，记录日志
                    logger.info(f"✓ Issue #{issue_num} 已关闭")
                    if not current_pr:
                        logger.warning(f"警告：Issue #{issue_num} 已关闭但未检测到关联的 PR")
                    return
            except Exception as e:
                logger.warning(f"获取 Issue 状态失败 (将重试): {e}")
                time.sleep(RETRY_SLEEP_SHORT)
                continue

            # 获取最新 PR
            try:
                pr_num = github.latest_pr_from_timeline(issue_num)
            except Exception as e:
                logger.warning(f"获取 PR 失败: {e}")
                time.sleep(RETRY_SLEEP_SHORT)
                continue

            # 修复：如果长时间没有 PR 创建，触发重置
            if not pr_num:
                elapsed_since_start = time.time() - wait_start_time
                if elapsed_since_start > PR_WAIT_TIMEOUT:
                    if reset_count >= DEFAULT_MAX_PR_RESETS:
                        raise RuntimeError(
                            f"等待 PR 创建超时 ({PR_WAIT_TIMEOUT/60:.1f}min)，"
                            f"且重置次数已达上限 ({DEFAULT_MAX_PR_RESETS})，Issue #{issue_num} 需要人工介入"
                        )

                    logger.warning(f"等待 PR 创建超时 ({elapsed_since_start/60:.1f}min)，触发重置 (第 {reset_count + 1}/{DEFAULT_MAX_PR_RESETS} 次)")
                    self._reset_issue(github, issue_num)
                    reset_count += 1
                    wait_start_time = time.time()  # 仅重置 PR 等待计时器
                    time.sleep(RESET_WAIT_TIME)
                    continue

            if pr_num:
                # 检测到新 PR
                if current_pr != pr_num:
                    current_pr = pr_num
                    pr_create_time = time.time()
                    # 注意：不重置 wait_start_time，它专门用于等待 PR 创建超时
                    logger.info(f"检测到 PR #{pr_num}")

                # 检查 PR 状态
                try:
                    pr = github.get_pull(pr_num)
                except Exception as e:
                    logger.warning(f"获取 PR 状态失败: {e}")
                    time.sleep(RETRY_SLEEP_SHORT)
                    continue

                # 如果已合并，完成
                if pr.get("merged_at"):
                    logger.info(f"✓ PR #{pr_num} 已合并")
                    return

                # 如果 PR 被外部关闭（未合并），重置
                if pr.get("state") == "closed":
                    logger.warning(f"PR #{pr_num} 已关闭但未合并")
                    if reset_count >= DEFAULT_MAX_PR_RESETS:
                        raise RuntimeError(f"PR 重置次数已达上限 ({DEFAULT_MAX_PR_RESETS})，Issue #{issue_num} 需要人工介入")

                    # 注意：reset_count 从 0 开始，所以这是第 (reset_count + 1) 次重置
                    logger.warning(f"重置流程 (第 {reset_count + 1}/{DEFAULT_MAX_PR_RESETS} 次)")
                    self._reset_issue(github, issue_num)
                    reset_count += 1
                    current_pr = None
                    pr_create_time = None
                    wait_start_time = time.time()  # 关键修复：重置等待计时器
                    time.sleep(RESET_WAIT_TIME)
                    continue

                # 条件1：检测到完成信号，立即标记为 ready 并合并 PR
                if check_copilot_signal(github, pr_num):
                    logger.info(f"✓ 检测到 copilot_work_finished 信号")

                    # 关键修复：无论当前状态如何，都尝试标记为 ready
                    # 因为 Copilot 完成后 PR 可能处于 "ready for review" 状态
                    # 需要显式调用 gh pr ready 才能合并
                    logger.info(f"尝试将 PR #{pr_num} 标记为 Ready 状态...")
                    try:
                        github.mark_pr_ready(pr_num)
                        logger.info(f"✓ 已将 PR #{pr_num} 标记为 Ready")
                        time.sleep(PR_READY_WAIT)
                    except Exception as e:
                        # 如果 PR 已经是 ready 状态，命令可能会失败，这是正常的
                        logger.debug(f"标记 Ready 时出现异常（可能 PR 已是 Ready 状态）: {e}")

                    try:
                        github.merge_pull(pr_num)
                        logger.info(f"✓ PR #{pr_num} 合并成功")
                        return
                    except Exception as e:
                        # 再次确认是否已合并
                        try:
                            pr_status = github.get_pull(pr_num)
                            if pr_status.get("merged_at"):
                                logger.info(f"✓ PR #{pr_num} 已合并")
                                return
                        except Exception:
                            pass

                        # 合并失败，检查是否可以重置
                        logger.error(f"合并 PR #{pr_num} 失败: {e}")
                        if reset_count >= DEFAULT_MAX_PR_RESETS:
                            raise RuntimeError(f"合并失败且重置次数已达上限 ({DEFAULT_MAX_PR_RESETS})，Issue #{issue_num} 需要人工介入: {e}") from e

                        # 触发重置，让 Copilot 重新处理
                        logger.warning(f"将重置流程并让 Copilot 重试 (第 {reset_count + 1}/{DEFAULT_MAX_PR_RESETS} 次)")
                        try:
                            merge_fail_comment = f"""❌ **PR 合并失败**

错误信息：{str(e)[:200]}

已关闭此 PR 并触发 Issue #{issue_num} 的重置流程。
重置次数：{reset_count + 1}/{DEFAULT_MAX_PR_RESETS}
"""
                            github.comment_issue(pr_num, merge_fail_comment)
                            github.close_pr(pr_num, delete_branch=True)
                        except Exception:
                            pass  # 关闭失败也继续
                        self._reset_issue(github, issue_num)
                        reset_count += 1
                        current_pr = None
                        pr_create_time = None
                        wait_start_time = time.time()
                        time.sleep(RESET_WAIT_TIME)
                        continue

                # 条件2：PR 超时，重置流程
                if pr_create_time:
                    elapsed = time.time() - pr_create_time
                    if elapsed > PR_TIMEOUT:
                        if reset_count >= DEFAULT_MAX_PR_RESETS:
                            logger.error(f"PR #{pr_num} 超时 ({elapsed/3600:.1f}h)，已达最大重置次数")
                            raise RuntimeError(f"PR 超时且重置次数已达上限 ({DEFAULT_MAX_PR_RESETS})，Issue #{issue_num} 需要人工介入")

                        logger.warning(f"PR #{pr_num} 超时 ({elapsed/3600:.1f}h / {PR_TIMEOUT/3600:.1f}h)，准备重置流程 (第 {reset_count + 1}/{DEFAULT_MAX_PR_RESETS} 次)")

                        # 关闭超时 PR（注意：关闭 PR 不会关闭 Issue，Issue 仍然保持 open）
                        try:
                            # 先添加评论说明超时原因，方便后期审计
                            timeout_comment = f"""🕒 **PR 超时自动关闭**

Copilot 处理时间超过 {PR_TIMEOUT/3600:.1f} 小时，自动关闭此 PR。
已触发 Issue #{issue_num} 的重置流程，Copilot 将重新处理任务。

重置次数：{reset_count + 1}/{DEFAULT_MAX_PR_RESETS}
"""
                            github.comment_issue(pr_num, timeout_comment)  # PR 也是一种 Issue
                            github.close_pr(pr_num, delete_branch=True)
                            logger.info(f"✓ 已关闭超时 PR #{pr_num} 并添加说明评论")
                        except Exception as e:
                            logger.warning(f"关闭 PR 失败（继续执行重置）: {e}")

                        self._reset_issue(github, issue_num)
                        reset_count += 1
                        current_pr = None
                        pr_create_time = None
                        wait_start_time = time.time()
                        logger.info(f"已触发重置，等待 {RESET_WAIT_TIME} 秒后继续监控")
                        time.sleep(RESET_WAIT_TIME)
                        continue

            # 心跳日志
            current_time = time.time()
            if current_time - last_heartbeat >= HEARTBEAT_INTERVAL:
                elapsed_mins = (current_time - issue_start_time) / 60
                reset_suffix = f" [重置:{reset_count}/{DEFAULT_MAX_PR_RESETS}]" if reset_count > 0 else ""

                if pr_num and pr_create_time:
                    pr_elapsed = (current_time - pr_create_time) / 60
                    pr_remaining = (PR_TIMEOUT - (current_time - pr_create_time)) / 60
                    indicator = "⏰" if pr_remaining < 30 else "⏳"
                    status = f"等待信号 (PR #{pr_num}, {pr_elapsed:.0f}/{PR_TIMEOUT/60:.0f}min, 剩余{pr_remaining:.0f}min){reset_suffix} {indicator}"
                else:
                    wait_elapsed = (current_time - wait_start_time) / 60
                    wait_remaining = (PR_WAIT_TIMEOUT - (current_time - wait_start_time)) / 60
                    status = f"等待 PR ({wait_elapsed:.0f}/{PR_WAIT_TIMEOUT/60:.0f}min, 剩余{wait_remaining:.0f}min){reset_suffix}"

                logger.info(f"💓 [{elapsed_mins:.0f}min] {status}")
                last_heartbeat = current_time

            time.sleep(self.args.poll_interval)

    def _reset_issue(self, github: GitHubClient, issue_num: int) -> None:
        """重置 Issue：通过 unassign + assign 触发 Copilot 重新处理"""
        try:
            # 检查 Issue 状态，如果已关闭则不重置
            issue_data = github.get_issue(issue_num)
            if issue_data.get("state") == "closed":
                logger.warning(f"Issue #{issue_num} 已关闭，跳过重置")
                return

            # 获取当前分配的用户列表
            assignees = {
                (assignee.get("login") or "").lower()
                for assignee in issue_data.get("assignees", []) if assignee
            }

            # 关键修复：先 unassign Copilot（如果已分配），再重新 assign
            # 这是触发 Copilot 重新处理的正确方式
            if COPILOT_USERNAME in assignees:
                logger.info(f"检测到 Copilot 已分配，先取消分配以触发重新处理")
                github.remove_assignees(issue_num, COPILOT_ASSIGNEES)
                time.sleep(2)  # 给 GitHub 一点时间处理 unassign

            # 重新分配 Copilot
            logger.info(f"重新分配 Issue #{issue_num} 给 Copilot")
            github.add_assignees(issue_num, COPILOT_ASSIGNEES)
            logger.info(f"✓ 已触发 Copilot 重新处理 Issue #{issue_num}")

        except Exception as e:
            logger.error(f"重置 Issue 失败: {e}")
            raise

    # ==================== 入口 ====================

def main() -> int:
    parser = argparse.ArgumentParser(description="Auto Copilot Pipeline - 自动续传版本")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                        help="轮询间隔（秒）")
    parser.add_argument("--issue-max-wait", type=int, default=DEFAULT_MAX_WAIT,
                        help="单个 Issue 最大等待时间（秒）")
    parser.add_argument("--issue-batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="每个 Issue 包含的 TODO 数量")
    parser.add_argument("--task-max-retries", type=int, default=DEFAULT_TASK_MAX_RETRIES,
                        help="单个工作项失败后的最大重试次数")
    parser.add_argument("--task-retry-wait", type=int, default=DEFAULT_TASK_RETRY_WAIT,
                        help="首次重试前的等待时间（秒），之后按重试序号递增")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式，不创建实际 Issue")
    parser.add_argument("--from-beginning", action="store_true",
                        help="强制从头开始，忽略 GitHub Issues 中的进度")
    parser.add_argument("--repo", type=str,
                        help="手动指定仓库 (格式: owner/repo)，覆盖自动检测")
    args = parser.parse_args()

    # 验证参数合理性
    if args.poll_interval < 1:
        logger.error("轮询间隔必须至少为 1 秒")
        return 1
    if args.issue_max_wait < 60:
        logger.error("Issue 超时必须至少为 60 秒")
        return 1
    if args.issue_batch_size < 1:
        logger.error("批次大小必须至少为 1")
        return 1
    if args.task_max_retries < 1:
        logger.error("任务最大重试次数必须至少为 1")
        return 1

    if args.repo:
        if "/" not in args.repo:
            logger.error("仓库格式错误，应为 owner/repo")
            return 1
        owner, repo = args.repo.split("/", 1)
    else:
        try:
            owner, repo = resolve_repo()
        except RuntimeError as e:
            if args.dry_run:
                logger.warning(f"DRY RUN 模式且无法检测仓库: {e}")
                logger.warning("将使用模拟仓库 dummy/repo 继续运行")
                owner, repo = "dummy", "repo"
            else:
                raise

    logger.info("="*80)
    logger.info("Auto Copilot Pipeline - 配置")
    logger.info("="*80)
    logger.info(f"仓库: {owner}/{repo}")
    logger.info(f"轮询间隔: {args.poll_interval}秒")
    logger.info(f"Issue 超时: {args.issue_max_wait}秒 ({args.issue_max_wait/3600:.1f}小时)")
    logger.info(f"批次大小: {args.issue_batch_size}")
    logger.info(f"任务最大重试: {args.task_max_retries} 次 (初始等待 {args.task_retry_wait}秒)")
    if args.dry_run:
        logger.info("模式: DRY RUN (预览)")
    logger.info("="*80)

    try:
        github: Optional[GitHubClient] = None
        if args.dry_run:
            logger.info("Dry-run 模式：跳过 GitHub 客户端初始化")
        else:
            github = GitHubClient(owner, repo)
        ensure_core_documents()
        pipeline = Pipeline(github, args)

        iteration = 0
        while True:
            try:
                iteration += 1

                completed_ids: set[str] = set()
                if not args.from_beginning:
                    completed_ids = pipeline.get_recent_completed_todos()

                work_items = iter_work_items(TODO_ROOT, args.issue_batch_size, completed_ids)

                if not work_items:
                    if iteration == 1:
                        logger.info("✓ 所有 TODO 已完成，无需进一步操作。")
                    # 如果是持续运行模式，且没有新任务，等待一段时间再扫描
                    if not args.dry_run:
                        logger.info(f"暂无待办任务，{args.poll_interval} 秒后重新扫描...")
                        time.sleep(args.poll_interval)
                        continue
                    break

                if iteration > 1:
                    logger.info("\n" + "="*80)
                    logger.info(f"自动续传：检测到新的任务批次 (第 {iteration} 轮)")
                    logger.info("="*80)

                pipeline.run(work_items)

                if args.dry_run:
                    logger.info("Dry-run 模式：首轮任务预览完成，自动退出。")
                    break

            except Exception as e:
                logger.error(f"\n✗ 流水线执行出错 (第 {iteration} 轮): {e}", exc_info=True)
                if args.dry_run:
                    raise  # Dry run 模式下直接报错退出

                # 无人值守模式：等待后重试
                wait_time = MAIN_ERROR_WAIT
                logger.info(f"将在 {wait_time} 秒后自动重试...")
                time.sleep(wait_time)
                continue

        logger.info("\n" + "="*80)
        logger.info("✓ 流水线执行成功完成")
        logger.info("="*80)
        return 0
    except KeyboardInterrupt:
        logger.warning("\n⚠ 用户中断执行")
        return 130
    except Exception as e:
        logger.error(f"\n✗ 致命错误: {e}", exc_info=True)
        return 1

def signal_handler(signum: int, frame: Any) -> None:
    """优雅退出信号处理器"""
    logger.warning(f"\n⚠ 收到信号 {signum}，正在优雅退出...")
    logger.info("提示：可以使用 --from-beginning 重新开始，或直接运行以从上次中断处继续")
    sys.exit(130)

if __name__ == "__main__":
    # 注册信号处理器（Ctrl+C 等）
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)

    sys.exit(main())
