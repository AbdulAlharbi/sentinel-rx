/* Sentinel-RX site: renders site/data.json (produced by `make report`). Vanilla JS + Chart.js. */
(async function () {
  const PALETTE = { agent: "#2a78d6", runbook: "#eb6834", single_shot: "#1baf7a", llm_only: "#eda100",
    no_verify: "#e87ba4", no_loop: "#008300", no_beacon: "#6250d6", no_safety: "#e34948" };
  const css = getComputedStyle(document.documentElement);
  const MUTED = css.getPropertyValue("--muted").trim() || "#52514e";
  const GRID = css.getPropertyValue("--grid").trim() || "#e6e5e1";
  Chart.defaults.color = MUTED;
  Chart.defaults.borderColor = GRID;
  Chart.defaults.font.size = 12;

  let data;
  try {
    data = await (await fetch("data.json")).json();
  } catch (e) {
    document.getElementById("results-meta").textContent =
      "data.json not found. Run `make report` and serve this folder (python -m http.server -d site).";
    return;
  }
  const pct = (m) => (m == null || isNaN(m.mean)) ? "n/a" : `${(100 * m.mean).toFixed(0)}% [${(100 * m.ci_lo).toFixed(0)}, ${(100 * m.ci_hi).toFixed(0)}]`;
  const num = (m, d = 2) => (m == null || isNaN(m.mean)) ? "n/a" : `${m.mean.toFixed(d)} [${m.ci_lo.toFixed(d)}, ${m.ci_hi.toFixed(d)}]`;
  const mainConfigs = ["agent", "runbook", "single_shot", "llm_only"].filter((c) => data.heldout[c]);
  const H = data.heldout;

  // ---- leaderboard -------------------------------------------------------------------
  if (mainConfigs.length) {
    const cols = [["top1", "top-1 root cause", pct, true], ["fault_recall", "per-fault recall", pct, true], ["recovered", "recovered", pct, true],
      ["false_recovery", "false 'recovered'", pct, false], ["harmful_actions", "harmful / run", num, false], ["unnecessary_actions", "unnecessary / run", num, false],
      ["isolated_before_restart", "isolate-before-restart", pct, true], ["llm_calls", "LLM calls", (m) => m.mean.toFixed(1), false], ["tokens", "tokens / incident", (m) => m.mean.toFixed(0), false]];
    const t = document.getElementById("leaderboard");
    t.innerHTML = "<thead><tr><th>config</th>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "<th>n</th></tr></thead>";
    const body = document.createElement("tbody");
    for (const c of mainConfigs) {
      const m = H[c].summary.metrics;
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><span class="tag" style="border-color:${PALETTE[c]};color:${PALETTE[c]}">${c}</span></td>` +
        cols.map(([k, , f, higher]) => {
          const vals = mainConfigs.map((cc) => H[cc].summary.metrics[k]?.mean ?? NaN).filter((v) => !isNaN(v));
          const best = higher === true ? Math.max(...vals) : (higher === false && ["false_recovery", "harmful_actions", "unnecessary_actions"].includes(k) ? Math.min(...vals) : null);
          const v = m[k]?.mean;
          return `<td class="${best != null && v === best ? "best" : ""}">${f(m[k])}</td>`;
        }).join("") + `<td>${H[c].summary.n_runs}</td>`;
      body.appendChild(tr);
    }
    t.appendChild(body);
    const meta = H.agent?.meta || H[mainConfigs[0]].meta;
    document.getElementById("results-meta").textContent =
      `Held-out split (${meta.n_scenarios} scenarios × ${meta.seeds} seeds), model ${meta.model}, git ${meta.git}. Bold = best per column. Every number comes from results/*.json.`;
  }

  // ---- charts --------------------------------------------------------------------------
  const base = { responsive: true, plugins: { legend: { position: "bottom" }, tooltip: { enabled: true } } };
  if (mainConfigs.length) {
    const metrics = [["top1", "top-1"], ["fault_recall", "recall"], ["recovered", "recovered"], ["false_recovery", "false recovered"]];
    new Chart(document.getElementById("chart-config"), {
      type: "bar",
      data: { labels: metrics.map((m) => m[1]), datasets: mainConfigs.map((c) => ({ label: c, backgroundColor: PALETTE[c], borderRadius: 4, borderSkipped: "bottom",
        data: metrics.map(([k]) => 100 * H[c].summary.metrics[k].mean) })) },
      options: { ...base, scales: { y: { min: 0, max: 100, title: { display: true, text: "% of runs" }, grid: { color: GRID } }, x: { grid: { display: false } } } },
    });
    new Chart(document.getElementById("chart-nfaults"), {
      type: "line",
      data: { labels: ["1 fault", "2 faults", "3 faults"], datasets: mainConfigs.map((c) => ({ label: c, borderColor: PALETTE[c], backgroundColor: PALETTE[c], pointRadius: 5, borderWidth: 2, tension: 0,
        data: ["1", "2", "3"].map((n) => H[c].summary.by_n_faults[n] ? 100 * H[c].summary.by_n_faults[n].recovered.mean : null) })) },
      options: { ...base, scales: { y: { min: 0, max: 100, title: { display: true, text: "recovered, % of runs" }, grid: { color: GRID } }, x: { grid: { display: false } } } },
    });
    new Chart(document.getElementById("chart-cost"), {
      type: "scatter",
      data: { datasets: [...mainConfigs, ...Object.keys(H).filter((c) => !mainConfigs.includes(c))].map((c) => ({ label: c, backgroundColor: PALETTE[c], pointRadius: 7,
        data: [{ x: H[c].summary.metrics.tokens.mean, y: 100 * H[c].summary.metrics.recovered.mean }] })) },
      options: { ...base, scales: { x: { title: { display: true, text: "LLM tokens per incident" }, grid: { color: GRID } }, y: { min: 0, max: 100, title: { display: true, text: "recovered, % of runs" }, grid: { color: GRID } } } },
    });
  }
  if (data.robustness && data.robustness.length && H.agent) {
    const rows = data.robustness;
    new Chart(document.getElementById("chart-robust"), {
      type: "bar",
      data: { labels: rows.map((r) => r.perturbation), datasets: [
        { label: "recovered (perturbed)", backgroundColor: PALETTE.agent, borderRadius: 4, data: rows.map((r) => 100 * r.recovered.perturbed) },
        { label: "top-1 (perturbed)", backgroundColor: "#9dbfe8", borderRadius: 4, data: rows.map((r) => 100 * r.top1.perturbed) },
        { label: "recovered (clean held-out)", type: "line", borderColor: PALETTE.agent, borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, data: rows.map(() => 100 * H.agent.summary.metrics.recovered.mean) },
      ] },
      options: { ...base, indexAxis: "y", scales: { x: { min: 0, max: 100, title: { display: true, text: "% of runs" }, grid: { color: GRID } }, y: { grid: { display: false } } } },
    });
    const rt = document.getElementById("robust-table");
    rt.innerHTML = "<thead><tr><th>perturbation</th><th>top-1</th><th>recovered</th><th>false recovered</th><th>harmful / run</th><th>drop in recovered</th><th>n</th></tr></thead><tbody>" +
      rows.map((r) => `<tr><td>${r.perturbation}</td><td>${(100 * r.top1.perturbed).toFixed(0)}%</td><td>${(100 * r.recovered.perturbed).toFixed(0)}%</td><td>${(100 * r.false_recovery.perturbed).toFixed(0)}%</td><td>${r.harmful_actions.perturbed.toFixed(2)}</td><td>${(100 * r.recovered.drop) > 0 ? "−" : "+"}${Math.abs(100 * r.recovered.drop).toFixed(0)} pts</td><td>${r.n_runs}</td></tr>`).join("") + "</tbody>";
  }
  if (data.ablations && data.ablations.length) {
    const rows = data.ablations;
    new Chart(document.getElementById("chart-ablate"), {
      type: "bar",
      data: { labels: rows.map((r) => r.ablation), datasets: [
        { label: "recovered (pts)", backgroundColor: PALETTE.agent, borderRadius: 4, data: rows.map((r) => 100 * r.recovered.delta) },
        { label: "false recovered (pts)", backgroundColor: PALETTE.runbook, borderRadius: 4, data: rows.map((r) => 100 * r.false_recovery.delta) },
        { label: "harmful actions (×100 / run)", backgroundColor: PALETTE.no_safety, borderRadius: 4, data: rows.map((r) => 100 * r.harmful_actions.delta) },
      ] },
      options: { ...base, scales: { y: { title: { display: true, text: "delta vs. full agent" }, grid: { color: GRID } }, x: { grid: { display: false } } } },
    });
  }
  if (data.beacon) {
    const b = data.beacon;
    document.getElementById("beacon-card").innerHTML = `<h3 style="margin:0 0 6px">Beacon detector (RandomForest)</h3>
      <p class="note">Synthetic held-out test, n = ${b.n_test}</p>
      <ul><li>precision <strong>${b.precision.toFixed(3)}</strong>, recall <strong>${b.recall.toFixed(3)}</strong>, AP <strong>${b.average_precision.toFixed(3)}</strong></li>
      <li>false-positive rate on health checks: <strong>${b.false_positive_rate_by_kind.health.toFixed(3)}</strong>, on update polls: <strong>${b.false_positive_rate_by_kind.update.toFixed(3)}</strong></li>
      <li>simulator dev scenarios: ${b.simulator_dev_eval.tp} beacons found, ${b.simulator_dev_eval.fp} false positives, ${b.simulator_dev_eval.fn} missed</li></ul>
      <img src="figures/beacon-detector-PR-curve.png" alt="Beacon detector precision-recall curve" style="max-width:100%;border-radius:6px">`;
  }

  // ---- replay viewer --------------------------------------------------------------------
  const traces = data.sample_traces || [];
  const sel = document.getElementById("trace-select");
  const body = document.getElementById("replay-body");
  const label = document.getElementById("step-label");
  let ti = 0, si = 0;
  traces.forEach((t, i) => {
    const gt = (t.ground_truth?.faults || []).map((f) => `${f.type}@${f.target}`).join(" + ");
    const o = document.createElement("option"); o.value = i; o.textContent = `${t.scenario_id} (seed ${t.seed_offset}): ${gt}`; sel.appendChild(o);
  });
  const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  function render() {
    if (!traces.length) { body.innerHTML = "<p class='note'>No traces yet: run the held-out benchmark and `make report`.</p>"; return; }
    const t = traces[ti];
    const iters = t.steps.filter((s) => s.phase === "iteration");
    const steps = iters.length + 1;
    si = Math.max(0, Math.min(si, steps - 1));
    label.textContent = si < iters.length ? `iteration ${si + 1} / ${iters.length}` : "outcome";
    if (si === iters.length) {
      const gt = t.ground_truth || {}; const sc = t.score || {};
      body.innerHTML = `<div class="step"><h3>Outcome</h3>
        <p>${t.claimed_recovered ? '<span class="tag ok">agent claims recovered</span>' : '<span class="tag warn">escalated</span>'}
        ${gt.recovered ? '<span class="tag ok">ground truth: recovered</span>' : '<span class="tag bad">ground truth: NOT recovered</span>'}
        ${sc.false_recovery ? '<span class="tag bad">false recovery claim</span>' : ""}
        ${sc.harmful_actions ? `<span class="tag bad">${sc.harmful_actions} harmful action(s)</span>` : '<span class="tag ok">no harmful actions</span>'}</p>
        <h4>Final hypotheses</h4><ul>${t.final_hypotheses.map((h) => `<li>${esc(h.service)} · ${esc(h.type)} · ${h.confidence.toFixed(2)} · <em>${esc(h.status)}</em></li>`).join("")}</ul>
        <h4>Ground-truth faults</h4><ul>${(gt.faults || []).map((f) => `<li>${esc(f.type)} on ${esc(f.target)} — ${f.resolved_at != null ? "resolved" : "still active"}${f.reinfections ? ` · ${f.reinfections} reinfection(s)` : ""}</li>`).join("")}</ul>
        <h4>Cost</h4><p>${t.llm_calls} LLM calls · ${t.tokens_in + t.tokens_out} tokens · ${t.llm_seconds.toFixed(0)} s of model time · ${t.env_calls} env calls</p></div>`;
      return;
    }
    const it = iters[si]; const ev = it.evidence || {};
    const ranked = (ev.ranked_services || []).map((r) => `<li>${esc(r.service)} score ${r.score}${r.explained_by?.length ? ` (symptoms explained by ${r.explained_by.join(", ")})` : ""}</li>`).join("");
    const anomalies = (ev.metric_anomalies || []).slice(0, 6).map((m) => `<li>${esc(m.service)}.${esc(m.metric)} ${m.direction} (${m.baseline} → ${m.recent}, z=${m.z})</li>`).join("");
    const logs = (ev.log_signatures || []).slice(0, 4).map((l) => `<li>${esc(l.service)} ${esc(l.level)}: <code>${esc(l.template)}</code> ×${l.recent}</li>`).join("");
    const beacons = (ev.beacons || []).map((b) => `<li><strong>${esc(b.src)} → ${esc(b.dst)}</strong> p=${b.p_beacon}, ${b.events} events every ~${b.interval_s}s (jitter ${b.jitter})${b.blocked_fraction ? `, ${(100 * b.blocked_fraction).toFixed(0)}% blocked` : ""}</li>`).join("");
    const hyps = (it.hypotheses || []).map((h) => `<li>${esc(h.service)} · ${esc(h.type)} · ${h.confidence.toFixed(2)}${h.rationale ? ` — <em>${esc(h.rationale)}</em>` : ""}</li>`).join("");
    const checks = (it.checks || []).map((c) => `<li>${esc(c.tool)}(${esc(c.service || "all")}${c.grep ? `, grep "${esc(c.grep)}"` : ""}) → ${c.count != null ? `${c.count} lines` : c.blocked != null ? `${(c.pairs || []).length} pairs, ${c.blocked} blocked` : "ok"}</li>`).join("");
    const acts = (it.actions || []).map((a) => {
      const cls = a.result === "applied" ? "ok" : a.result === "blocked" ? "bad" : "warn";
      return `<li><span class="tag ${cls}">${esc(a.result)}</span> ${esc(a.action.name)}(${esc(a.action.target)})${a.reason ? ` — ${esc(a.reason)}` : ""}${a.message ? ` — <em>${esc(a.message)}</em>` : ""}${a.health_before != null ? ` · health ${a.health_before} → ${a.health_after}` : ""}</li>`;
    }).join("");
    const v = it.verification || {};
    body.innerHTML = `<div class="step"><h3>${esc(t.scenario_id)} — iteration ${si + 1}</h3>
      <h4>1 · Observe (detector evidence, t=${ev.t}s, health ${ev.health})</h4><ul>${ranked || "<li>nothing ranked</li>"}</ul>
      <ul>${anomalies}${logs}${beacons}</ul>
      <h4>2 · LLM hypotheses</h4><ul>${hyps || "<li>none</li>"}</ul>
      <h4>3 · Targeted checks</h4><ul>${checks || "<li>none</li>"}</ul>
      <h4>4 · Plan → safety gate → actions</h4><p class="note">plan: ${(it.plan || []).join(", ") || "none"}</p><ul>${acts || "<li>no actions</li>"}</ul>
      <h4>5 · Verify (after ${v.waited_s != null ? v.waited_s.toFixed(0) : "?"} s)</h4>
      <p>${v.problems && v.problems.length ? '<span class="tag bad">not recovered</span> ' + v.problems.map(esc).join("; ") : v.skipped ? '<span class="tag warn">verification skipped (ablation)</span>' : '<span class="tag ok">recovered: telemetry matches the healthy baseline, no beacon attempts</span>'}</p></div>`;
  }
  sel.addEventListener("change", () => { ti = +sel.value; si = 0; render(); });
  document.getElementById("prev").addEventListener("click", () => { si--; render(); });
  document.getElementById("next").addEventListener("click", () => { si++; render(); });
  render();
  document.getElementById("footer-meta").textContent = `Generated from site/data.json (git ${data.generated_git}) by make report. ${(data.heldout_run_log || []).length} held-out runs are logged in results/heldout_runs.log.`;
})();
