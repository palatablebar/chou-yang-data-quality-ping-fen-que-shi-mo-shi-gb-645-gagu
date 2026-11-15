/* 采信 / 评分 / 补发：一个按钮跑完一条链路，失败就地提示并保留已填内容。 */
(function () {
  "use strict";

  const state = { busy: false };

  function byId(id) {
    return document.getElementById(id);
  }

  function status(node, text, kind) {
    if (!node) return;
    node.textContent = text;
    node.className = "status" + (kind ? " status--" + kind : "");
  }

  function buttons(disabled) {
    [byId("run-btn"), byId("replay-btn")].forEach(function (button) {
      if (button) button.disabled = disabled;
    });
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const message = payload.message || payload.detail || response.statusText;
      throw new Error(message + "（HTTP " + response.status + "）");
    }
    return payload;
  }

  async function runPipeline(body) {
    if (state.busy) return;
    state.busy = true;
    buttons(true);
    status(byId("form-status"), "采信中…");
    try {
      await postJSON("/api/datasets/import", {
        sample: body.sample,
        encoding: body.encoding || null
      });
      status(byId("form-status"), "评分中…");
      const scored = await postJSON("/api/score", {
        sample: body.sample,
        encoding: body.encoding || null,
        release: body.release || "run",
        cutoff_at: body.cutoff_at || null,
        expected_version: body.expected_version
      });
      status(byId("form-status"), "完成，正在刷新…", "good");
      window.location.assign("/?run_id=" + encodeURIComponent(scored.run.run_id));
    } catch (error) {
      status(byId("form-status"), error.message, "bad");
      buttons(false);
    } finally {
      state.busy = false;
    }
  }

  function supplementBody(form) {
    const id = String((byId("supplement-id-input") || {}).value || "").trim();
    const raw = String((byId("supplement-ops-input") || {}).value || "").trim();
    if (!id) throw new Error("先填批次号");
    if (!raw) throw new Error("先填操作 JSON");
    let ops;
    try {
      ops = JSON.parse(raw);
    } catch (error) {
      throw new Error("操作不是合法 JSON：" + error.message);
    }
    return {
      supplement_id: id,
      dataset: form.getAttribute("data-dataset"),
      base_run_id: form.getAttribute("data-baseline"),
      ops: ops
    };
  }

  function renderPreview(node, payload) {
    if (!node) return;
    const score = payload.impact.score;
    const missing = payload.impact.missing;
    node.hidden = false;
    node.textContent = "预览：" + score.before + " → " + score.after +
      " 分（" + score.delta + "）；缺失格 " + missing.missing_before + " → " +
      missing.missing_after + "（" + missing.missing_delta + "）。未写入库。";
  }

  function bind() {
    const form = byId("ingest-form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        const data = new FormData(form);
        runPipeline({
          sample: String(data.get("sample") || ""),
          encoding: String(data.get("encoding") || ""),
          release: String(data.get("release") || "run"),
          cutoff_at: String(data.get("cutoff_at") || ""),
          expected_version: null
        });
      });
    }
    const replay = byId("replay-btn");
    if (replay && form) {
      replay.addEventListener("click", function () {
        const release = String((byId("release-select") || {}).value || "run");
        runPipeline({
          sample: form.getAttribute("data-sample") || "",
          encoding: String((byId("encoding-input") || {}).value || ""),
          release: release,
          cutoff_at: String((byId("cutoff-input") || {}).value || ""),
          expected_version: form.getAttribute("data-version") || null
        });
      });
    }

    const supplement = byId("supplement-form");
    if (supplement) {
      const preview = byId("preview-btn");
      if (preview) {
        preview.addEventListener("click", async function () {
          const node = byId("supplement-status");
          try {
            status(node, "试算中…");
            const payload = await postJSON("/api/supplements/preview",
                                           supplementBody(supplement));
            renderPreview(byId("preview-result"), payload);
            status(node, "预览完成，未写入", "good");
          } catch (error) {
            status(node, error.message, "bad");
          }
        });
      }
      supplement.addEventListener("submit", async function (event) {
        event.preventDefault();
        const node = byId("supplement-status");
        try {
          status(node, "应用中…");
          const payload = await postJSON("/api/supplements",
                                         supplementBody(supplement));
          status(node, "完成，正在刷新…", "good");
          window.location.assign("/?run_id=" +
            encodeURIComponent(payload.run.run_id));
        } catch (error) {
          status(node, error.message, "bad");
        }
      });
    }

    document.querySelectorAll(".btn--restore").forEach(function (button) {
      button.addEventListener("click", async function () {
        const node = byId("restore-status");
        const releaseId = button.getAttribute("data-release");
        if (!releaseId) return;
        if (!window.confirm("确定把生效指针切回 " + releaseId +
                            "？不会删除任何数据。")) {
          return;
        }
        button.disabled = true;
        status(node, "校验并恢复中…");
        try {
          await postJSON("/api/releases/" + encodeURIComponent(releaseId) +
                         "/activate", {});
          status(node, "已恢复，正在刷新…", "good");
          window.location.reload();
        } catch (error) {
          status(node, error.message, "bad");
          button.disabled = false;
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
