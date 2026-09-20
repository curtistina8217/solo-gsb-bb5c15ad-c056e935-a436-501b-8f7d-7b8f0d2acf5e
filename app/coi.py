"""利益冲突（COI）自助申报：一次性链接的签发、校验与合并。

安全模型
========
* 令牌为 256 位随机串，**只在签发响应中完整出现一次**；
  数据库仅保存其 SHA-256 哈希，任何查询接口与日志都不得回传哈希或明文令牌。
* 链接绑定签发时的稿件清单快照（id + 标题的有序列表）及其摘要哈希；
  签发后稿件增删或标题变化 → 摘要不匹配 → 链接失效，
  打开与提交均返回 409 且不写入，管理员可重新生成链接。
* 提交在**单个事务**中完成「合并冲突 + 链接置为已提交」：
  要么全部落库要么整体回滚；重复提交返回明确错误，绝不部分写入。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime

from .db import transaction

# 数据库中持久化的链接状态
PENDING = "pending"      # 待提交
SUBMITTED = "submitted"  # 已提交
REVOKED = "revoked"      # 已撤销

# 管理端展示状态（动态计算，不落库）
NOT_ISSUED = "not_issued"    # 未签发
INVALIDATED = "invalidated"  # 已失效（被撤销，或签发后稿件清单已变化）


class CoiError(Exception):
    """COI 流程错误：HTTP 状态码 + 用户可读信息（由 API 层转成 HTTPException）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    """令牌的 SHA-256 哈希（数据库中唯一保存的令牌形态）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def manuscript_snapshot(conn: sqlite3.Connection) -> list[dict]:
    """当前稿件清单快照：仅 id 与标题，按 id 升序。"""
    return [
        {"id": r["id"], "title": r["title"]}
        for r in conn.execute("SELECT id, title FROM manuscripts ORDER BY id")
    ]


def snapshot_digest(snapshot: list[dict]) -> str:
    """快照的规范摘要：稿件增删或标题变化都会改变它。"""
    canon = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- 管理端操作
def issue_link(conn: sqlite3.Connection, r_id: int) -> str:
    """为评审人签发一次性申报链接，返回明文令牌（仅此处出现一次，不入库、不入日志）。

    每位评审人至多一条链接记录：重新签发会替换旧记录，旧令牌立即失效。
    """
    token = secrets.token_urlsafe(32)
    snap = manuscript_snapshot(conn)
    conn.execute(
        "INSERT INTO coi_links(r_id, token_hash, snapshot, snapshot_hash, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(r_id) DO UPDATE SET token_hash = excluded.token_hash, "
        "snapshot = excluded.snapshot, snapshot_hash = excluded.snapshot_hash, "
        "status = excluded.status, created_at = excluded.created_at, submitted_at = NULL",
        (r_id, hash_token(token), json.dumps(snap, ensure_ascii=False),
         snapshot_digest(snap), PENDING, _now()),
    )
    return token


def revoke_link(conn: sqlite3.Connection, r_id: int) -> None:
    """撤销待提交链接；未签发、已撤销或已提交时给出明确错误。"""
    row = conn.execute("SELECT status FROM coi_links WHERE r_id = ?", (r_id,)).fetchone()
    if row is None:
        raise CoiError(404, "该评审人尚未签发申报链接。")
    if row["status"] == SUBMITTED:
        raise CoiError(409, "该链接已提交，无法撤销。")
    if row["status"] == REVOKED:
        raise CoiError(409, "该链接已撤销，请勿重复操作。")
    conn.execute("UPDATE coi_links SET status = ? WHERE r_id = ?", (REVOKED, r_id))


def reviewer_status(conn: sqlite3.Connection, r_id: int,
                    current_digest: str | None = None) -> dict:
    """管理端展示的申报状态：未签发 / 待提交 / 已提交 / 已失效。

    绝不包含明文令牌或令牌哈希。
    """
    row = conn.execute(
        "SELECT status, snapshot_hash, created_at, submitted_at "
        "FROM coi_links WHERE r_id = ?",
        (r_id,),
    ).fetchone()
    if row is None:
        return {"status": NOT_ISSUED}
    base = {"created_at": row["created_at"]}
    if row["status"] == SUBMITTED:
        return {"status": SUBMITTED, **base, "submitted_at": row["submitted_at"]}
    if row["status"] == REVOKED:
        return {"status": INVALIDATED, "reason": "revoked", **base}
    # 待提交：签发后稿件清单有变化则视为已失效
    if current_digest is None:
        current_digest = snapshot_digest(manuscript_snapshot(conn))
    if current_digest != row["snapshot_hash"]:
        return {"status": INVALIDATED, "reason": "stale", **base}
    return {"status": PENDING, **base}


# ----------------------------------------------------------------- 评审人侧
def _get_link_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM coi_links WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    if row is None:
        raise CoiError(404, "申报链接无效或已被替换，请联系管理员重新获取。")
    return row


def _check_usable(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """链接可用性检查：已提交 / 已撤销 / 快照过期分别给出明确错误。"""
    if row["status"] == SUBMITTED:
        raise CoiError(409, "该链接已完成提交，请勿重复提交；如需修改请联系管理员。")
    if row["status"] == REVOKED:
        raise CoiError(410, "该链接已被管理员撤销，请联系管理员重新获取。")
    current = snapshot_digest(manuscript_snapshot(conn))
    if current != row["snapshot_hash"]:
        raise CoiError(
            409,
            "签发后稿件清单已变化（稿件增删或标题修改），本链接已失效；"
            "请联系管理员重新生成链接。",
        )


def get_form(conn: sqlite3.Connection, token: str) -> dict:
    """评审人打开链接：仅返回评审人姓名与稿件标题清单（不含任何其他数据）。"""
    row = _get_link_by_token(conn, token)
    _check_usable(conn, row)
    reviewer = conn.execute(
        "SELECT name FROM reviewers WHERE id = ?", (row["r_id"],)
    ).fetchone()
    return {
        "reviewer_name": reviewer["name"] if reviewer else f"#{row['r_id']}",
        "manuscripts": json.loads(row["snapshot"]),
    }


def submit(conn: sqlite3.Connection, token: str, manuscript_ids: list[int]) -> dict:
    """提交申报：单事务合并冲突并立即失效链接；任何失败都整体回滚、不部分写入。"""
    row = _get_link_by_token(conn, token)
    _check_usable(conn, row)
    snapshot_ids = {m["id"] for m in json.loads(row["snapshot"])}
    ids = sorted({int(i) for i in manuscript_ids})
    unknown = [i for i in ids if i not in snapshot_ids]
    if unknown:
        raise CoiError(400, f"勾选的稿件不在本次申报清单内：{unknown}")
    with transaction(conn):
        # 合并进现有冲突矩阵：已存在的冲突保持不动，不删除未勾选的既有冲突
        for m_id in ids:
            conn.execute(
                "INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)",
                (m_id, row["r_id"]),
            )
        # 仅当仍处于待提交状态才置为已提交；并发重复提交时影响行数为 0 → 回滚
        cur = conn.execute(
            "UPDATE coi_links SET status = ?, submitted_at = ? WHERE id = ? AND status = ?",
            (SUBMITTED, _now(), row["id"], PENDING),
        )
        if cur.rowcount != 1:
            raise CoiError(409, "该链接已完成提交，请勿重复提交。")
    return {"merged": len(ids)}
