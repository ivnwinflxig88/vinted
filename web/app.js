"use strict";

const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
const byId = (id) => document.getElementById(id);
let connected = false;
let busy = false;
let ended = false;
let rows = [];

function message(text, error = false) {
  byId("message").textContent = text;
  byId("message").classList.toggle("error", error);
}

function controls() {
  document.querySelectorAll("button, input, select").forEach((el) => {
    el.disabled = busy || ended;
  });
  byId("refresh").disabled = busy || ended || !connected;
  byId("create").disabled = busy || ended || !connected || !rows.some((row) => !row.profile_id);
  document.querySelectorAll("button[data-profile]").forEach((button) => {
    button.disabled = busy || ended || !connected || !rows.find((row) => row.name === button.dataset.profile)?.profile_id;
  });
}

function render() {
  const labels = { existing: "已存在 · 保留原设置", missing: "尚未创建", created: "本次已创建" };
  byId("profiles").replaceChildren();
  rows.forEach((row, index) => {
    const card = document.createElement("article");
    const heading = document.createElement("h3");
    heading.textContent = row.name;
    const note = document.createElement("p");
    note.textContent = `第 ${index + 1} 个账户 · ${labels[row.state]}`;
    const id = document.createElement("small");
    id.textContent = row.profile_id ? `环境 ID：${row.profile_id}` : "创建后可打开";
    const button = document.createElement("button");
    button.className = "secondary";
    button.textContent = "打开环境，手动登录";
    button.dataset.profile = row.name;
    button.addEventListener("click", () => run(async () => {
      const result = await request("/api/start", { name: row.name });
      message(result.message);
    }, `正在打开 ${row.name}，首次下载浏览器可能需要更长时间……`));
    card.append(heading, note, id, button);
    byId("profiles").append(card);
  });
  controls();
}

async function request(path, body = {}) {
  if (!token) throw new Error("本机会话已失效。请关闭此页，重新运行启动.command；不要手动刷新旧页面。");
  let response;
  try {
    response = await fetch(path, {
      method: "POST", credentials: "omit", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Local-Token": token },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("本机工具连接已中断。请检查终端是否仍在运行。创建可能已生效，请重新连接并检查环境列表。");
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作未完成，请重新检查环境列表。");
  return result;
}

async function run(action, progress) {
  if (busy || ended) return;
  busy = true;
  controls();
  message(progress);
  try { await action(); }
  catch (error) { message(error.message, true); }
  finally { busy = false; controls(); }
}

byId("connection").addEventListener("submit", (event) => {
  event.preventDefault();
  const key = byId("key").value;
  byId("key").value = "";
  run(async () => {
    connected = false;
    rows = [];
    render();
    const result = await request("/api/connect", { port: byId("port").value, key, site: byId("site").value });
    connected = true;
    rows = result.rows;
    render();
    message("已连接。先查看四个环境的状态，再点击「创建缺少的环境」。API Key 已从输入框清除。");
  }, "正在连接并读取环境列表……");
});

byId("create").addEventListener("click", () => {
  const count = rows.filter((row) => !row.profile_id).length;
  if (!window.confirm(`将创建 ${count} 个缺少的环境，已有同名环境保持原样。继续？`)) return;
  run(async () => {
    const result = await request("/api/create");
    rows = result.rows;
    render();
    message(result.error || "四个环境已齐全。请按顺序点击各环境的「打开环境」，在浏览器中手动登录。", !!result.error);
  }, "正在逐个创建缺少的环境，请保持 AdsPower 和终端开启……");
});

byId("refresh").addEventListener("click", () => run(async () => {
  const result = await request("/api/plan");
  rows = result.rows;
  render();
  message("环境列表已更新。已有同名环境的账户请在浏览器内自行确认。");
}, "正在重新检查环境列表……"));

byId("exit").addEventListener("click", () => run(async () => {
  const result = await request("/api/exit");
  ended = true;
  byId("key").value = "";
  message(result.message);
}, "正在退出……"));

if (!token) {
  ended = true;
  message("请通过「启动.command」打开新的管理页面；旧页面刷新后会失去本机会话。", true);
}
controls();
