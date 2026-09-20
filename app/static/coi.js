/* 利益冲突自助申报页（评审人侧）：凭一次性链接访问，仅展示稿件标题并勾选冲突 */
"use strict";

const token = location.pathname.replace(/\/+$/, "").split("/").pop();
const app = document.getElementById("coi-app");
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function showError(msg) {
  app.innerHTML = `<div class="alert danger">⛔ ${esc(msg)}</div>`;
}

async function load() {
  try {
    const res = await fetch(`/api/coi/${encodeURIComponent(token)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `加载失败（${res.status}）`);
    renderForm(data);
  } catch (e) {
    showError(e.message);
  }
}

function renderForm(data) {
  const items = data.manuscripts.map((m) => `
    <label class="coi-item">
      <input type="checkbox" value="${m.id}" /> 《${esc(m.title)}》
    </label>`).join("");
  app.innerHTML = `
    <p>尊敬的 <strong>${esc(data.reviewer_name)}</strong>：</p>
    <p class="hint">请勾选与您存在利益冲突的稿件（仅展示稿件标题），然后点击提交。
      提交后勾选项将并入冲突记录，且本链接立即失效、不可重复提交。</p>
    <form id="coi-form">
      ${items || '<p class="hint">当前没有需要申报的稿件。</p>'}
      <button class="btn primary" type="submit">提交申报</button>
    </form>`;
  document.getElementById("coi-form").addEventListener("submit", submitForm);
}

async function submitForm(e) {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  const ids = [...e.target.querySelectorAll("input[type=checkbox]:checked")]
    .map((cb) => Number(cb.value));
  btn.disabled = true;
  try {
    const res = await fetch(`/api/coi/${encodeURIComponent(token)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manuscript_ids: ids }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `提交失败（${res.status}）`);
    app.innerHTML = `<div class="alert success">✅ 申报已提交（本次合并 ${data.merged} 条冲突记录）。
      本链接已失效，感谢配合。</div>`;
  } catch (err) {
    showError(err.message);
  }
}

load();
