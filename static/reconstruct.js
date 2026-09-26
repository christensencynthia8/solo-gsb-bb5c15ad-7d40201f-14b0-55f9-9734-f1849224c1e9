"use strict";

// 剪辑字幕重建：粘贴原 SRT 与 JSON 剪辑数组（source_in/source_out/target_in，
// 毫秒），字幕与每段素材区间求交、裁切后按原速平移到目标时间轴；预览目标
// 时间码、原块解析序位与正文（保留换行），下载从 1 编号的无重叠 SRT。
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    srt: $("rcSrt"),
    cuts: $("rcCuts"),
    runBtn: $("rcRunBtn"),
    clearBtn: $("rcClearBtn"),
    status: $("rcStatus"),
    summary: $("rcSummary"),
    resultWrap: $("rcResultWrap"),
    body: $("rcBody"),
    downloadBtn: $("rcDownloadBtn"),
  };

  const SAMPLE_SRT = [
    "1",
    "00:00:01,000 --> 00:00:03,000",
    "你好，世界！",
    "Hello, world!",
    "",
    "2",
    "00:00:04,000 --> 00:00:06,000",
    "今天我们来聊聊剪辑重建。",
    "",
    "3",
    "00:00:07,000 --> 00:00:09,000",
    "片段可以乱序，也可以重复使用。",
    "",
    "4",
    "00:00:10,000 --> 00:00:12,000",
    "目标区间允许重叠。",
    "",
    "5",
    "00:00:13,000 --> 00:00:15,000",
    "这一句不会被任何片段取到。",
    "",
  ].join("\n");

  const SAMPLE_CUTS = [
    "[",
    "  { \"source_in\": 0,     \"source_out\": 5000,  \"target_in\": 0 },",
    "  { \"source_in\": 9000,  \"source_out\": 13000, \"target_in\": 5000 },",
    "  { \"source_in\": 4000,  \"source_out\": 8000,  \"target_in\": 9000 },",
    "  { \"source_in\": 9000,  \"source_out\": 11000, \"target_in\": 8000 }",
    "]",
  ].join("\n");

  $("rcSample").addEventListener("click", () => {
    els.srt.value = SAMPLE_SRT;
    els.cuts.value = SAMPLE_CUTS;
  });

  els.clearBtn.addEventListener("click", () => {
    els.srt.value = "";
    els.cuts.value = "";
    els.summary.classList.add("hidden");
    els.resultWrap.classList.add("hidden");
    setStatus("", false);
  });

  function setStatus(msg, isError) {
    els.status.textContent = msg;
    els.status.classList.toggle("error", !!isError);
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function multilineCell(text) {
    return escapeHtml(text).replace(/\n/g, '<span class="br-marker">↵</span><br>');
  }

  // 前端预校验剪辑数组；非法时指出项号，禁止发起预览/下载（后端仍会再校验）。
  function parseCuts(text) {
    const trimmed = text.trim();
    if (!trimmed) throw new Error("请填写剪辑数组 JSON");
    let data;
    try {
      data = JSON.parse(trimmed);
    } catch (e) {
      throw new Error(`剪辑数组不是合法 JSON：${e.message}`);
    }
    if (!Array.isArray(data)) {
      throw new Error("剪辑数组必须是 JSON 数组（每项为一个剪辑区间对象）");
    }
    if (!data.length) throw new Error("剪辑数组为空：至少需要一个剪辑区间");
    data.forEach((item, i) => {
      const label = `剪辑清单第 ${i + 1} 项`;
      if (item === null || typeof item !== "object" || Array.isArray(item)) {
        throw new Error(`${label}必须是 JSON 对象，含 source_in/source_out/target_in`);
      }
      for (const key of ["source_in", "source_out", "target_in"]) {
        if (!(key in item)) throw new Error(`${label}缺少字段 ${key}`);
        const v = item[key];
        if (typeof v !== "number" || !Number.isInteger(v)) {
          throw new Error(`${label}的 ${key} 必须是非负整数毫秒`);
        }
        if (v < 0) {
          throw new Error(`${label}的 ${key} 必须是非负整数毫秒（当前为 ${v}）`);
        }
      }
      if (item.source_out <= item.source_in) {
        throw new Error(
          `${label}出点 ${item.source_out} 须大于入点 ${item.source_in}` +
          "（区间左闭右开，不可为空）"
        );
      }
    });
    return data;
  }

  function render(data) {
    const s = data.summary;
    $("rcCnt").textContent = s.total;
    $("rcCutsShown").textContent = s.cuts;
    $("rcBlocks").textContent = s.blocks;

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const tr = document.createElement("tr");
      tr.className = "row-match";
      const positions = r.block_positions
        .map((p) => `<span class="pos-tag">${p}</span>`)
        .join(" ");
      tr.innerHTML = `
        <td class="cell-idx">${r.new_index}</td>
        <td class="cell-tc cell-tc-new">${escapeHtml(r.start_tc)} → ${escapeHtml(r.end_tc)}</td>
        <td class="cell-rc-pos">${positions}</td>
        <td class="cell-text cell-multiline">${multilineCell(r.text)}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    let cuts;
    try {
      cuts = parseCuts(els.cuts.value);
    } catch (e) {
      setStatus(e.message, true);
      return;
    }
    lastRequest = { srt: els.srt.value, cuts };

    els.runBtn.disabled = true;
    setStatus("重建中……", false);
    try {
      const resp = await fetch("/api/reconstruct", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `请求失败（${resp.status}）` }));
        throw new Error(err.detail || `请求失败（${resp.status}）`);
      }
      const data = await resp.json();
      render(data);
      setStatus(
        `完成：${data.summary.total} 段重建字幕（${data.summary.cuts} 个剪辑段）`,
        false
      );
    } catch (e) {
      lastRequest = null;
      els.summary.classList.add("hidden");
      els.resultWrap.classList.add("hidden");
      setStatus(e.message, true);
    } finally {
      els.runBtn.disabled = false;
    }
  }

  async function downloadSrt() {
    if (!lastRequest) {
      setStatus("请先执行重建", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/reconstruct.srt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "下载失败" }));
        throw new Error(err.detail || "下载失败");
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "reconstruct.srt";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      els.downloadBtn.disabled = false;
    }
  }

  els.runBtn.addEventListener("click", run);
  els.downloadBtn.addEventListener("click", downloadSrt);
})();
