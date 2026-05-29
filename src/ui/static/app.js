let state = {
  nodes: [],
  conversations: [],
  currentConversationId: null,
  currentConversation: null,
  inspectorOpen: false,
  inspectorWidth: 390,
  selectedTraceId: null,
  selectedTrace: null,
  selectedTraceLabel: "",
  contextPreviewTimer: null,
  contextPreviewSeq: 0,
};

const els = {
  appShell: document.querySelector(".app-shell"),
  nodeSelect: document.querySelector("#node-select"),
  appAlert: document.querySelector("#app-alert"),
  conversationList: document.querySelector("#conversation-list"),
  messages: document.querySelector("#messages"),
  contextMeter: document.querySelector("#context-meter"),
  prompt: document.querySelector("#prompt"),
  form: document.querySelector("#chat-form"),
  send: document.querySelector("#send"),
  refresh: document.querySelector("#refresh"),
  newChat: document.querySelector("#new-chat"),
  deleteChat: document.querySelector("#delete-chat"),
  inspectorResize: document.querySelector("#inspector-resize"),
  routingInspector: document.querySelector("#routing-inspector"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function showAppAlert(message, type = "error") {
  if (!els.appAlert) {
    return;
  }
  els.appAlert.textContent = message;
  els.appAlert.className = `app-alert ${type}`;
  els.appAlert.hidden = false;
}

function clearAppAlert() {
  if (!els.appAlert) {
    return;
  }
  els.appAlert.textContent = "";
  els.appAlert.className = "app-alert";
  els.appAlert.hidden = true;
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function markdownToHtml(markdown) {
  const lines = String(markdown || "").split("\n");
  const blocks = [];
  let paragraph = [];
  let list = [];
  let code = [];
  let math = [];
  let inCode = false;
  let inMath = false;

  function flushParagraph() {
    if (paragraph.length) {
      blocks.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  }

  function flushList() {
    if (list.length) {
      blocks.push(`<ul>${list.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
      list = [];
    }
  }

  for (const line of lines) {
    const trimmed = line.trim();

    if (trimmed.startsWith("$$")) {
      if (inMath) {
        const remainder = trimmed.slice(2).trim();
        if (remainder) {
          math.push(remainder);
        }
        blocks.push(`<div class="math-block">$$${escapeHtml(math.join("\n"))}$$</div>`);
        math = [];
        inMath = false;
      } else if (trimmed.endsWith("$$") && trimmed.length > 4) {
        flushParagraph();
        flushList();
        blocks.push(
          `<div class="math-block">$$${escapeHtml(trimmed.slice(2, -2).trim())}$$</div>`
        );
      } else {
        flushParagraph();
        flushList();
        const remainder = trimmed.slice(2).trim();
        if (remainder) {
          math.push(remainder);
        }
        inMath = true;
      }
      continue;
    }

    if (inMath) {
      if (trimmed.endsWith("$$")) {
        math.push(trimmed.slice(0, -2).trim());
        blocks.push(`<div class="math-block">$$${escapeHtml(math.join("\n"))}$$</div>`);
        math = [];
        inMath = false;
      } else {
        math.push(line);
      }
      continue;
    }

    if (line.trim().startsWith("```")) {
      if (inCode) {
        blocks.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = [];
        inCode = false;
      } else {
        flushParagraph();
        flushList();
        inCode = true;
      }
      continue;
    }

    if (inCode) {
      code.push(line);
      continue;
    }

    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      list.push(bullet[1]);
      continue;
    }

    flushList();
    paragraph.push(trimmed);
  }

  if (inCode) {
    blocks.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  }
  if (inMath) {
    blocks.push(`<div class="math-block">$$${escapeHtml(math.join("\n"))}$$</div>`);
  }
  flushParagraph();
  flushList();
  return blocks.join("");
}

function formatScore(value) {
  if (typeof value !== "number") {
    return "n/a";
  }
  return value.toFixed(2);
}

function formatDuration(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) {
    return "n/a";
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  if (ms < 10000) {
    return `${(ms / 1000).toFixed(1)} s`;
  }
  return `${Math.round(ms / 1000)} s`;
}

function shortId(value) {
  const text = String(value || "");
  if (text.length <= 14) {
    return text;
  }
  return `${text.slice(0, 6)}...${text.slice(-4)}`;
}

function stepDurationText(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) {
    return null;
  }
  return `Took ${formatDuration(ms)}`;
}

function hopDurationText(hop, field) {
  return stepDurationText(hop?.[field]);
}

function forwardTimingText(hop, timing) {
  return hopDurationText(hop, "forward_duration_ms") || timing?.forward;
}

function recommendationTimingText(hop) {
  if (typeof hop.recommendation_duration_ms === "number") {
    return stepDurationText(hop.recommendation_duration_ms);
  }
  if (hop.recommendation_status === "disabled") {
    return "Not enabled";
  }
  if (hop.recommendation_status === "skipped") {
    return "Skipped";
  }
  if (hop.recommendation_status === "no_requests") {
    return "No request sent";
  }
  return null;
}

function traceProgressEvents(trace = {}) {
  return Array.isArray(trace.progress_events) ? trace.progress_events : [];
}

function traceTotalDurationMs(trace = {}) {
  const events = traceProgressEvents(trace).filter((event) => typeof event.ts === "number");
  if (events.length < 2) {
    return null;
  }
  return (events[events.length - 1].ts - events[0].ts) * 1000;
}

function eventLabel(eventName) {
  const labels = {
    classification_completed: "Classify capability needs",
    classification_reused: "Reuse capability needs",
    direct_candidates_evaluated: "Find and score direct candidates",
    recommendations_completed: "Ask peers for recommendations",
    recommended_candidates_evaluated: "Score recommended candidates",
    no_suitable_node: "Confirm no suitable node",
    forward_response_received: "Forwarded request round trip",
    forward_failed: "Forward attempt",
    forwarding_exhausted: "Try forward candidates",
    generation_completed: "Generate answer",
    generation_failed: "Generate answer",
  };
  return labels[eventName] || null;
}

function timingEndEvent(startEvent, events, startIndex) {
  const endEvents = {
    query_received: [
      "query_completed",
      "generation_failed",
      "routing_failed",
      "no_suitable_node",
      "forwarding_exhausted",
    ],
    classification_started: ["classification_completed"],
    dht_lookup_started: ["direct_candidates_evaluated"],
    recommendations_started: ["recommendations_completed"],
    recommendations_completed: ["recommended_candidates_evaluated"],
    forward_started: [
      "forward_response_received",
      "forward_failed",
      "forwarding_exhausted",
    ],
    generation_started: ["generation_completed", "generation_failed"],
  };
  const names = endEvents[startEvent.event] || [];
  return events
    .slice(startIndex + 1)
    .find((candidate) => names.includes(candidate.event) && candidate.ts >= startEvent.ts);
}

function forwardedGenerationDurationMs(trace = {}) {
  for (const hop of trace.hops || []) {
    if (
      (hop.action === "execute_forwarded_request" ||
        hop.action === "forwarded_generation_error") &&
      typeof hop.generation_duration_ms === "number"
    ) {
      return hop.generation_duration_ms;
    }
  }
  return null;
}

function hasForwardedAnswer(trace = {}) {
  const hops = trace.hops || [];
  return (
    hops.some((hop) => hop.action === "forward") &&
    hops.some(
      (hop) =>
        hop.action === "execute_forwarded_request" ||
        hop.action === "forwarded_generation_error"
    )
  );
}

function timingRowMetadata(startEvent, endEvent, trace = {}) {
  if (endEvent.event === "recommendations_completed") {
    const statuses = {
      used: "used",
      skipped: "skipped",
      no_requests: "none sent",
      disabled: "disabled",
    };
    const status = statuses[endEvent.status] || "";
    const requestCount =
      typeof endEvent.request_count === "number" ? `${endEvent.request_count} req` : "";
    return [requestCount, status].filter(Boolean).join(", ");
  }
  if (
    endEvent.event === "direct_candidates_evaluated" ||
    endEvent.event === "recommended_candidates_evaluated"
  ) {
    return typeof endEvent.candidate_count === "number"
      ? `${endEvent.candidate_count} candidates`
      : "";
  }
  if (endEvent.event === "forward_response_received" || endEvent.event === "forward_failed") {
    const peer = endEvent.target_peer_id ? `peer ${shortId(endEvent.target_peer_id)}` : "";
    const timings = endEvent.timings || {};
    const wait =
      typeof timings.response_wait_duration_ms === "number"
        ? `wait ${formatDuration(timings.response_wait_duration_ms)}`
        : "includes peer processing";
    const generationMs = forwardedGenerationDurationMs(trace);
    const generation =
      generationMs !== null ? `generation ${formatDuration(generationMs)} included` : "";
    return [peer, wait, generation].filter(Boolean).join(", ");
  }
  return "";
}

function measuredTimingRows(events, trace = {}) {
  const rows = [];
  const starts = new Set([
    "classification_started",
    "dht_lookup_started",
    "recommendations_started",
    "recommendations_completed",
    "forward_started",
    "generation_started",
  ]);

  for (const [index, event] of events.entries()) {
    if (event.event === "classification_reused") {
      rows.push({
        label: eventLabel(event.event),
        durationMs: 0,
        elapsedMs: (event.ts - events[0].ts) * 1000,
        meta: "already known",
      });
      continue;
    }

    if (!starts.has(event.event)) {
      continue;
    }
    if (event.event === "generation_started" && hasForwardedAnswer(trace)) {
      continue;
    }

    const endEvent = timingEndEvent(event, events, index);
    const label = endEvent ? eventLabel(endEvent.event) : null;
    if (!endEvent || !label) {
      continue;
    }

    const durationMs = (endEvent.ts - event.ts) * 1000;
    const isRecommendationWithoutWork =
      endEvent.event === "recommendations_completed" &&
      (!endEvent.request_count || endEvent.status === "no_requests" || endEvent.status === "disabled");

    if (durationMs < 1 || isRecommendationWithoutWork) {
      continue;
    }

    rows.push({
      label,
      durationMs,
      elapsedMs: (endEvent.ts - events[0].ts) * 1000,
      meta: timingRowMetadata(event, endEvent, trace),
    });
  }

  return rows;
}

function traceTimingSummary(trace = {}) {
  const events = traceProgressEvents(trace).filter((event) => typeof event.ts === "number");
  if (!events.length) {
    return null;
  }

  const rows = measuredTimingRows(events, trace);
  return {
    totalMs: events.length > 1 ? (events[events.length - 1].ts - events[0].ts) * 1000 : 0,
    rows,
  };
}

function makeTimingLookup(trace = {}) {
  const events = traceProgressEvents(trace).filter((event) => typeof event.ts === "number");
  const byEvent = new Map();

  for (const event of events) {
    if (!byEvent.has(event.event)) {
      byEvent.set(event.event, event);
    }
  }

  function phase(startName, endNames) {
    const start = byEvent.get(startName);
    const end = endNames.map((name) => byEvent.get(name)).find(Boolean);
    if (!start || !end || end.ts < start.ts) {
      return null;
    }
    return stepDurationText((end.ts - start.ts) * 1000);
  }

  return {
    phase,
    routing: phase("routing_started", [
      "routing_decision",
      "routing_failed",
      "no_suitable_node",
    ]),
    request: phase("query_received", ["routing_started", "generation_started"]),
    decision: phase("routing_decision", [
      "forward_started",
      "generation_started",
      "no_suitable_node",
    ]),
    forward: phase("forward_started", [
      "forward_response_received",
      "forward_failed",
      "forwarding_exhausted",
    ]),
    generation: phase("generation_started", [
      "generation_completed",
      "generation_failed",
      "query_completed",
    ]),
    complete: phase("query_completed", []),
  };
}

function formatCapabilities(scores = {}) {
  const entries = Object.entries(scores);
  if (!entries.length) {
    return "none";
  }
  return entries.map(([capability, score]) => `${capability} ${formatScore(score)}`).join(", ");
}

function capabilityList(peer = {}) {
  const names = peer.advertised_capabilities || [];
  if (!names.length) {
    return "none";
  }
  return names.join(", ");
}

function contributionLabel(value) {
  if (typeof value !== "number") {
    return "n/a";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}`;
}

function addField(parent, label, value) {
  const row = document.createElement("div");
  row.className = "trace-field";

  const name = document.createElement("span");
  name.className = "trace-field-label";
  name.textContent = label;

  const text = document.createElement("span");
  text.className = "trace-field-value";
  text.textContent = value || "n/a";

  row.append(name, text);
  parent.appendChild(row);
}

function makeDetails(title) {
  const details = document.createElement("details");
  details.className = "trace-details";

  const summary = document.createElement("summary");
  summary.textContent = title;
  details.appendChild(summary);
  return details;
}

function messagesScrollState() {
  const distanceFromBottom =
    els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight;
  return {
    top: els.messages.scrollTop,
    nearBottom: distanceFromBottom < 80,
  };
}

function restoreMessagesScroll(scrollState) {
  if (scrollState.nearBottom) {
    els.messages.scrollTop = els.messages.scrollHeight;
  } else {
    els.messages.scrollTop = scrollState.top;
  }
}

function traceDetailOpenKeys() {
  return new Set(
    [...els.routingInspector.querySelectorAll(".trace-details")]
      .map((details, index) => {
        const title = details.querySelector("summary")?.textContent || "";
        return details.open ? `${index}:${title}` : null;
      })
      .filter(Boolean)
  );
}

function restoreTraceDetailOpenState(openKeys) {
  for (const [index, details] of [
    ...els.routingInspector.querySelectorAll(".trace-details"),
  ].entries()) {
    const title = details.querySelector("summary")?.textContent || "";
    details.open = openKeys.has(`${index}:${title}`);
  }
}

function nodeDisplayName(peer = {}) {
  return peer.model_name || peer.peer_id || "unknown node";
}

function nodeIdentityText(peer = {}) {
  const name = nodeDisplayName(peer);
  return peer.peer_id && peer.peer_id !== name ? `${name} (${peer.peer_id})` : name;
}

function describeAction(action) {
  const descriptions = {
    forward: "This node selected another node, then forwarded the query to it.",
    forward_failed: "This node selected another node, but the forward attempt failed.",
    execute_local: "This node selected itself as the best candidate. It will generate the answer in the final step below.",
    execute_forwarded_request: "This is the final answer step: this node was selected earlier and now answers the query.",
    no_suitable_node: "Could not find a suitable node.",
    forward_error: "The selected peer returned an error.",
    forwarding_exhausted: "All forward attempts failed, so routing stops.",
    forwarded_generation_error: "The selected peer failed while generating an answer.",
    generation_error: "This node failed while generating an answer.",
  };
  return descriptions[action] || "Processed this query step.";
}

function selectionReasonText(hop) {
  if (hop.selection_reason === "direct_candidate_above_threshold") {
    return "A direct candidate reached the minimum routing score, so it was selected without asking for recommendations.";
  }
  if (hop.selection_reason === "recommended_candidate_above_threshold") {
    return "No direct candidate reached the minimum routing score, then a recommended candidate did, so it was selected.";
  }
  if (hop.selection_reason === "best_seen_below_threshold") {
    return "No candidate reached the minimum routing score, so the best evaluated candidate was selected.";
  }
  return "The selected node is the best candidate found for this routing step.";
}

function stageLabel(name) {
  if (name === "direct") {
    return "Direct candidates";
  }
  if (name === "recommended") {
    return "Recommended candidates";
  }
  return `${name || "unknown"} candidates`;
}

function stageDescription(name) {
  if (name === "direct") {
    return "Local node plus peers discovered through the DHT/registry are scored first.";
  }
  if (name === "recommended") {
    return "If no direct candidate passes the threshold, peers are asked to suggest nodes. Suggested nodes are then scored here.";
  }
  return "Candidates scored during this stage.";
}

function makeStepHeader(number, title, description, timingText = null) {
  const header = document.createElement("div");
  header.className = "trace-step-header";

  const badge = document.createElement("span");
  badge.className = "trace-step-badge";
  badge.textContent = String(number);

  const text = document.createElement("div");
  const label = document.createElement("div");
  label.className = "trace-step-title";
  label.textContent = title;
  const detail = document.createElement("div");
  detail.className = "trace-step-description";
  detail.textContent = description;

  text.append(label, detail);
  const timing = document.createElement("div");
  timing.className = "trace-step-time";
  timing.textContent = timingText || "";
  text.appendChild(timing);
  header.append(badge, text);
  return header;
}

function makeTimelineStep(number, title, description, timingText = null) {
  const section = document.createElement("section");
  section.className = "trace-step";
  section.appendChild(makeStepHeader(number, title, description, timingText));
  return section;
}

function appendPreviousAttemptNote(parent, hop) {
  if (!hop.previous_attempt) {
    return;
  }

  const note = document.createElement("div");
  note.className = "trace-note trace-request-note";
  note.textContent =
    `No candidate was found for the original needs ` +
    `(${formatCapabilities(hop.previous_attempt.required_capabilities || {})}). ` +
    `The same discovery and scoring process is retried with ` +
    `${formatCapabilities(hop.required_capabilities || {})}.`;
  parent.appendChild(note);
}

function isLocalAnswerAction(action) {
  return action === "execute_local" || action === "execute_local_after_forward_failure";
}

function makeNodeTitle(prefix, peer) {
  const wrapper = document.createElement("div");
  wrapper.className = "trace-node-title";

  const name = document.createElement("div");
  name.className = "trace-node-name";
  name.textContent = `${prefix}${nodeDisplayName(peer)}`;
  wrapper.appendChild(name);

  if (peer?.peer_id) {
    const id = document.createElement("div");
    id.className = "trace-node-id";
    id.textContent = peer.peer_id;
    wrapper.appendChild(id);
  }

  return wrapper;
}

function makeScoreBreakdown(breakdown) {
  if (!breakdown || !Object.keys(breakdown).length) {
    return null;
  }

  const labels = {
    capability_match: "Capability match",
    fresh_profile: "Fresh profile",
    recent_failures: "Recent failures",
    latency: "Latency",
  };

  const box = document.createElement("div");
  box.className = "trace-score-breakdown";

  const title = document.createElement("div");
  title.className = "trace-score-title";
  title.textContent = "Routing score components";
  box.appendChild(title);

  const help = document.createElement("div");
  help.className = "trace-score-help";
  help.textContent =
    "The final routing score starts from the requested-capability match, then adjusts for freshness, failures, and latency.";
  box.appendChild(help);

  for (const key of ["capability_match", "fresh_profile", "recent_failures", "latency"]) {
    const item = breakdown[key];
    if (!item) {
      continue;
    }
    const row = document.createElement("div");
    row.className = "trace-score-row";
    row.textContent = `${labels[key]}: value ${formatScore(item.value)} x weight ${formatScore(item.weight)} = ${contributionLabel(item.contribution)}`;
    box.appendChild(row);
  }

  return box;
}

function makeTraceCandidate(candidate) {
  const row = document.createElement("div");
  row.className = `trace-candidate${candidate.selected ? " selected" : ""}${
    candidate.kind === "local" ? " local" : ""
  }`;

  const peer = candidate.peer || {};
  const title = document.createElement("div");
  title.className = "trace-candidate-title";
  if (candidate.selected && candidate.kind === "local") {
    title.appendChild(makeNodeTitle("Selected local node: ", peer));
  } else if (candidate.selected) {
    title.appendChild(makeNodeTitle("Selected node: ", peer));
  } else if (candidate.kind === "local") {
    title.appendChild(makeNodeTitle("Local node: ", peer));
  } else {
    title.appendChild(makeNodeTitle("", peer));
  }

  const fields = document.createElement("div");
  fields.className = "trace-fields";
  addField(fields, "Candidate type", candidate.kind === "local" ? "local node" : "remote peer");
  addField(fields, "Routing score", formatScore(candidate.routing_score));
  addField(fields, "Requested-capability match", formatScore(candidate.weighted_quality));
  addField(fields, "Scores used for this request", formatCapabilities(candidate.node_scores || {}));
  if ((candidate.recommended_by || []).length) {
    addField(
      fields,
      "Recommended by",
      candidate.recommended_by
        .map((item) => `${nodeIdentityText(item.source_peer)} for ${item.capability}`)
        .join(", ")
    );
  }

  row.append(title, fields);
  const details = makeDetails("More candidate details");
  const detailFields = document.createElement("div");
  detailFields.className = "trace-fields";
  addField(detailFields, "Peer ID", peer.peer_id || "unknown");
  addField(detailFields, "Source", candidate.source || "unknown");
  addField(detailFields, "Full node scores", formatCapabilities(peer.capability_scores || {}));
  addField(detailFields, "Advertised capabilities", capabilityList(peer));
  details.appendChild(detailFields);

  const breakdown = makeScoreBreakdown(candidate.score_breakdown);
  if (breakdown) {
    details.appendChild(breakdown);
  }
  if (details.children.length > 1) {
    row.appendChild(details);
  }
  return row;
}

function makeSkippedCandidate(candidate) {
  const row = document.createElement("div");
  row.className = `trace-candidate skipped${
    candidate.kind === "local" ? " local" : ""
  }`;

  const peer = candidate.peer || {};
  const title = document.createElement("div");
  title.className = "trace-candidate-title";
  title.appendChild(
    makeNodeTitle(
      candidate.kind === "local" ? "Local node not evaluated: " : "Candidate not evaluated: ",
      peer
    )
  );

  const fields = document.createElement("div");
  fields.className = "trace-fields";
  addField(fields, "Peer ID", peer.peer_id || "unknown");
  addField(fields, "Candidate type", candidate.kind === "local" ? "local node" : "remote peer");
  addField(fields, "Reason", candidate.reason || "Not eligible for this stage.");

  row.append(title, fields);
  return row;
}

function makeDecisionReason(hop) {
  const reason = document.createElement("div");
  reason.className = "trace-reason";

  const title = document.createElement("div");
  title.className = "trace-reason-title";
  title.textContent = "Decision";
  reason.appendChild(title);

  const fields = document.createElement("div");
  fields.className = "trace-fields";

  const selected = hop.selected || {};
  const peer = selected.peer || hop.node || {};
  if (hop.action === "forward") {
    addField(fields, "Outcome", "Forwarded to selected peer");
    addField(fields, "Selected peer", nodeIdentityText(peer));
  } else if (hop.action === "execute_local") {
    addField(fields, "Outcome", "Selected local node");
    addField(fields, "Selected node", nodeDisplayName(peer));
  } else if (hop.action === "execute_forwarded_request") {
    addField(fields, "Outcome", "Executed after being selected by a previous hop");
    addField(fields, "Selected node", nodeDisplayName(peer));
  } else {
    addField(fields, "Outcome", describeAction(hop.action));
  }

  addField(fields, "Required scores", formatCapabilities(hop.required_capabilities || {}));
  addField(fields, "Why", selectionReasonText(hop));
  if (selected.node_scores) {
    addField(fields, "Scores used for this request", formatCapabilities(selected.node_scores));
  }
  if (typeof selected.routing_score === "number") {
    addField(fields, "Routing score", formatScore(selected.routing_score));
  }

  reason.appendChild(fields);

  const details = makeDetails("More decision details");
  const detailFields = document.createElement("div");
  detailFields.className = "trace-fields";
  addField(detailFields, "Minimum routing score", formatScore(hop.routing_score_threshold));
  if (selected.peer?.capability_scores) {
    addField(detailFields, "Full node scores", formatCapabilities(selected.peer.capability_scores));
  }
  if (typeof selected.weighted_quality === "number") {
    addField(detailFields, "Requested-capability match", formatScore(selected.weighted_quality));
  }
  if (!selected.peer && hop.decision_reason) {
    addField(detailFields, "Raw reason", hop.decision_reason);
  }
  details.appendChild(detailFields);
  reason.appendChild(details);
  return reason;
}

function appendHopFields(parent, hop, options = {}) {
  const fields = document.createElement("div");
  fields.className = "trace-fields";
  addField(fields, "Node", nodeDisplayName(hop.node));
  if (hop.routed_by_peer_id) {
    addField(fields, "Selected by", hop.routed_by_peer_id);
  }
  if (options.showNeeds && hop.required_capabilities) {
    addField(fields, "Required capabilities", formatCapabilities(hop.required_capabilities || {}));
  }
  if (options.showDht && hop.discovery_capabilities) {
    addField(fields, "DHT lookups", (hop.discovery_capabilities || []).join(", ") || "none");
  }
  parent.appendChild(fields);

  if (hop.node?.peer_id || (options.showThreshold && typeof hop.routing_score_threshold === "number")) {
    const details = makeDetails("Technical details");
    const detailFields = document.createElement("div");
    detailFields.className = "trace-fields";
    addField(detailFields, "Peer ID", hop.node?.peer_id || "unknown");
    if (options.showThreshold && typeof hop.routing_score_threshold === "number") {
      addField(detailFields, "Minimum routing score", formatScore(hop.routing_score_threshold));
    }
    details.appendChild(detailFields);
    parent.appendChild(details);
  }
}

function appendStageDetails(parent, stage, hop) {
  if (stage.name === "recommended" && (hop.recommendation_requests || []).length) {
    const requestNote = document.createElement("div");
    requestNote.className = "trace-note trace-request-note";
    requestNote.textContent =
      "Recommendation requests are discovery only: the asked node returns candidate peer IDs, but does not answer the user query.";
    parent.appendChild(requestNote);

    const requestList = document.createElement("div");
    requestList.className = "trace-request-list";

    for (const request of hop.recommendation_requests) {
      const requestRow = document.createElement("div");
      requestRow.className = "trace-request";
      addField(requestRow, "Capability", request.capability);
      addField(
        requestRow,
        "Asked node",
        request.source_peer ? nodeIdentityText(request.source_peer) : request.source_peer_id
      );
      addField(
        requestRow,
        "Returned peers",
        (request.returned_peer_ids || []).join(", ") || "none"
      );
      requestList.appendChild(requestRow);
    }

    parent.appendChild(requestList);
  }

  const candidates = stage.candidates || [];
  if (!candidates.length && !(stage.skipped_candidates || []).length) {
    const empty = document.createElement("div");
    empty.className = "trace-note";
    empty.textContent = "No reachable candidates in this step.";
    parent.appendChild(empty);
  }

  for (const candidate of candidates) {
    parent.appendChild(makeTraceCandidate(candidate));
  }

  for (const candidate of stage.skipped_candidates || []) {
    parent.appendChild(makeSkippedCandidate(candidate));
  }
}

function stageStepText(stage) {
  if (stage.name === "direct") {
    return {
      title: "Check direct candidates",
      description: "The entry node scores itself and peers it already knows from the DHT/registry.",
    };
  }
  if (stage.name === "recommended") {
    return {
      title: "Ask for recommendations",
      description: "Because direct candidates were not good enough, peers are asked to suggest additional candidates.",
    };
  }
  return {
    title: stageLabel(stage.name),
    description: stageDescription(stage.name),
  };
}

function makeAnswerStep(stepNumber, hop, timing) {
  const timingText =
    (hop.routed_by_peer_id ? null : hopDurationText(hop, "generation_duration_ms")) ||
    (hop.routed_by_peer_id ? null : timing?.generation);
  const section = makeTimelineStep(
    stepNumber,
    "Generate answer",
    hop.routed_by_peer_id
      ? "The selected node produces the final answer inside the forwarded request round trip."
      : "The selected local node produces the final answer.",
    timingText
  );
  appendHopFields(section, hop);
  return section;
}

function makeForwardFailureStep(stepNumber, hop, timing) {
  const section = makeTimelineStep(
    stepNumber,
    "Forward failed",
    "The selected node did not answer, so it is marked unreachable and excluded before routing is retried.",
    forwardTimingText(hop, timing)
  );
  const fields = document.createElement("div");
  fields.className = "trace-fields";
  addField(fields, "Attempt", hop.attempt || "unknown");
  addField(fields, "Failed peer", hop.failed_peer_id || hop.selected?.peer?.peer_id || "unknown");
  addField(fields, "Error", hop.forward_error || "unknown error");
  section.appendChild(fields);
  return section;
}

function makeTimingSummaryCard(trace) {
  const summary = traceTimingSummary(trace);
  if (!summary) {
    return null;
  }

  const section = document.createElement("section");
  section.className = "trace-timing-card";

  const title = document.createElement("div");
  title.className = "trace-timing-title";
  title.textContent = "Timing";
  section.appendChild(title);

  const fields = document.createElement("div");
  fields.className = "trace-fields";
  addField(fields, "Total", formatDuration(summary.totalMs));
  section.appendChild(fields);

  const note = document.createElement("div");
  note.className = "trace-timing-note";
  note.textContent =
    "Measured inside the network request. Browser-to-DIAL Chat time is excluded. " +
    "Forwarded request round trip includes selected-peer processing and generation.";
  section.appendChild(note);

  if (summary.rows.length) {
    const details = makeDetails("Show measured phases");
    const list = document.createElement("div");
    list.className = "trace-timing-list";
    for (const row of summary.rows) {
      const item = document.createElement("div");
      item.className = "trace-timing-row";
      const duration = document.createElement("div");
      duration.className = "trace-timing-duration";
      duration.textContent = formatDuration(row.durationMs);

      const body = document.createElement("div");
      body.className = "trace-timing-body";
      const label = document.createElement("div");
      label.className = "trace-timing-label";
      label.textContent = row.label;
      body.appendChild(label);

      if (row.meta) {
        const meta = document.createElement("div");
        meta.className = "trace-timing-meta";
        meta.textContent = row.meta;
        body.appendChild(meta);
      }

      item.append(duration, body);
      list.appendChild(item);
    }
    details.appendChild(list);
    section.appendChild(details);
  }
  return section;
}

function makeRoutingTrace(trace) {
  const details = document.createElement("div");
  details.className = "routing-trace";
  const timing = makeTimingLookup(trace);

  let stepNumber = 1;
  for (const [index, hop] of (trace?.hops || []).entries()) {
    if (hop.action === "execute_forwarded_request") {
      details.appendChild(makeAnswerStep(stepNumber, hop, timing));
      stepNumber += 1;
      continue;
    }

    if (hop.action === "forwarding_exhausted") {
      const exhaustedStep = makeTimelineStep(
        stepNumber,
        "Stop routing",
        "All forward attempts failed. The node returns no suitable answer instead of waiting longer.",
        forwardTimingText(hop, timing)
      );
      appendHopFields(exhaustedStep, hop);
      details.appendChild(exhaustedStep);
      stepNumber += 1;
      continue;
    }

    const receiveStep = makeTimelineStep(
      stepNumber,
      index === 0 ? "Entry node receives request" : "Continue routing",
      index === 0
        ? "The node selected in the UI has received the query and starts handling it."
        : "Routing continues after a previous forwarding attempt failed.",
      index === 0 ? timing.request : timing.routing
    );
    appendHopFields(receiveStep, hop, { showNeeds: true });
    appendPreviousAttemptNote(receiveStep, hop);
    details.appendChild(receiveStep);
    stepNumber += 1;

    for (const stage of hop.stages || []) {
      const text = stageStepText(stage);
      const stageStep = makeTimelineStep(
        stepNumber,
        `${text.title} (${stage.candidate_count || 0})`,
        text.description,
        stepDurationText(stage.duration_ms)
      );
      if (stage.name === "direct") {
        appendHopFields(stageStep, hop, { showDht: true, showThreshold: true });
      }
      appendStageDetails(stageStep, stage, hop);
      details.appendChild(stageStep);
      stepNumber += 1;
    }

    if (hop.recommendation_reason) {
      const recommendationStep = makeTimelineStep(
        stepNumber,
        "Recommendation result",
        hop.recommendation_reason,
        recommendationTimingText(hop)
      );
      details.appendChild(recommendationStep);
      stepNumber += 1;
    }

    if (hop.selected) {
      const decisionStep = makeTimelineStep(
        stepNumber,
        "Choose answering node",
        selectionReasonText(hop),
        timing.decision || timing.routing
      );
      decisionStep.appendChild(makeDecisionReason(hop));
      details.appendChild(decisionStep);
      stepNumber += 1;
    }

    if (hop.action === "forward" && hop.selected?.peer) {
      const forwardStep = makeTimelineStep(
        stepNumber,
        "Forwarded request round trip",
        "The entry node sends the query and waits for the selected node's full response, including generation.",
        forwardTimingText(hop, timing)
      );
      const fields = document.createElement("div");
      fields.className = "trace-fields";
      addField(fields, "From", nodeDisplayName(hop.node));
      addField(fields, "To", nodeIdentityText(hop.selected.peer));
      forwardStep.appendChild(fields);
      details.appendChild(forwardStep);
      stepNumber += 1;
    }

    if (hop.action === "forward_failed") {
      details.appendChild(makeForwardFailureStep(stepNumber, hop, timing));
      stepNumber += 1;
    }

    if (isLocalAnswerAction(hop.action)) {
      details.appendChild(makeAnswerStep(stepNumber, hop, timing));
      stepNumber += 1;
    }
  }

  return details;
}

function makeTraceButton(traceId, trace, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "routing-chip";
  button.dataset.traceId = traceId;
  if (state.inspectorOpen && state.selectedTraceId === traceId) {
    button.classList.add("active");
  }
  button.title = "Show routing details in the side panel";

  const answeredBy = trace?.answered_by;
  const name = answeredBy ? nodeDisplayName(answeredBy) : "unknown node";
  const totalMs = traceTotalDurationMs(trace);
  const meta = [
    `answered by ${name}`,
    totalMs !== null ? formatDuration(totalMs) : null,
  ].filter(Boolean);
  button.textContent = `Routing details - ${meta.join(" - ")}`;
  button.addEventListener("click", () => selectRoutingTrace(traceId, trace, label));
  return button;
}

function selectRoutingTrace(traceId, trace, label) {
  state.inspectorOpen = true;
  state.selectedTraceId = traceId;
  state.selectedTrace = trace;
  state.selectedTraceLabel = label;
  renderTraceInspector();
  syncTraceButtons();
}

function closeRoutingInspector() {
  state.inspectorOpen = false;
  renderTraceInspector();
  syncTraceButtons();
}

function resetTraceSelection() {
  state.inspectorOpen = false;
  state.selectedTraceId = null;
  state.selectedTrace = null;
  state.selectedTraceLabel = "";
}

function setInspectorWidth(width) {
  const maxWidth = Math.max(320, window.innerWidth - 620);
  state.inspectorWidth = Math.max(300, Math.min(width, Math.min(720, maxWidth)));
  els.appShell.style.setProperty("--inspector-width", `${state.inspectorWidth}px`);
}

function resizeInspector(event) {
  setInspectorWidth(window.innerWidth - event.clientX);
}

function startInspectorResize(event) {
  event.preventDefault();
  els.appShell.classList.add("resizing-inspector");
  els.inspectorResize.setPointerCapture(event.pointerId);
  resizeInspector(event);
}

function stopInspectorResize(event) {
  els.appShell.classList.remove("resizing-inspector");
  if (els.inspectorResize.hasPointerCapture(event.pointerId)) {
    els.inspectorResize.releasePointerCapture(event.pointerId);
  }
}

function syncTraceButtons() {
  for (const button of document.querySelectorAll(".routing-chip")) {
    button.classList.toggle(
      "active",
      state.inspectorOpen && button.dataset.traceId === state.selectedTraceId
    );
  }
}

function renderTraceInspector() {
  const openDetailKeys = traceDetailOpenKeys();
  const inspectorScrollTop = els.routingInspector.scrollTop;
  els.appShell.classList.toggle("inspector-open", state.inspectorOpen);
  setInspectorWidth(state.inspectorWidth);
  els.routingInspector.innerHTML = "";

  if (!state.inspectorOpen || !state.selectedTrace) {
    const empty = document.createElement("div");
    empty.className = "inspector-empty";
    empty.textContent = "Select a response's routing details to inspect the network decision.";
    els.routingInspector.appendChild(empty);
    return;
  }

  const header = document.createElement("header");
  header.className = "inspector-header";

  const heading = document.createElement("div");
  heading.className = "inspector-heading";

  const title = document.createElement("h2");
  title.textContent = "Network details";

  const close = document.createElement("button");
  close.type = "button";
  close.className = "secondary inspector-close";
  close.textContent = "Close";
  close.addEventListener("click", closeRoutingInspector);

  heading.append(title, close);

  const context = document.createElement("div");
  context.className = "inspector-context";
  const answeredBy = state.selectedTrace?.answered_by;
  const totalMs = traceTotalDurationMs(state.selectedTrace);
  const contextParts = [state.selectedTraceLabel];
  if (answeredBy) {
    contextParts.push(`answered by ${nodeDisplayName(answeredBy)}`);
  }
  if (totalMs !== null) {
    contextParts.push(`network time ${formatDuration(totalMs)}`);
  }
  context.textContent = contextParts.join(" - ");

  header.append(heading, context);
  els.routingInspector.appendChild(header);
  const timingCard = makeTimingSummaryCard(state.selectedTrace);
  if (timingCard) {
    els.routingInspector.appendChild(timingCard);
  }
  els.routingInspector.appendChild(makeRoutingTrace(state.selectedTrace));
  restoreTraceDetailOpenState(openDetailKeys);
  els.routingInspector.scrollTop = inspectorScrollTop;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function renderNodes() {
  els.nodeSelect.innerHTML = "";
  for (const node of state.nodes) {
    const option = document.createElement("option");
    option.value = node.api_url || "";
    option.textContent = node.label;
    option.disabled = !node.api_url;
    els.nodeSelect.appendChild(option);
  }
}

function renderConversations() {
  els.conversationList.innerHTML = "";
  for (const conversation of state.conversations) {
    const button = document.createElement("button");
    button.className = "conversation-item";
    if (conversation.id === state.currentConversationId) {
      button.classList.add("active");
    }
    button.textContent = conversation.title;
    button.addEventListener("click", () => {
      selectConversation(conversation.id).catch((error) => {
        showAppAlert(error.message || "Could not open the conversation.", "error");
      });
    });
    els.conversationList.appendChild(button);
  }
}

function renderMessages() {
  const scrollState = messagesScrollState();
  els.messages.innerHTML = "";
  const messages = state.currentConversation?.messages || [];
  if (!messages.length) {
    resetTraceSelection();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Start a new chat or select a saved conversation.";
    els.messages.appendChild(empty);
    renderTraceInspector();
    return;
  }

  const traces = [];
  let assistantIndex = 0;

  for (const [index, message] of messages.entries()) {
    const wrapper = document.createElement("article");
    const role = message.error ? "error" : message.role === "User" ? "user" : "assistant";
    wrapper.className = `message ${role}`;
    if (role === "assistant") {
      assistantIndex += 1;
    }
    if (message.pending) {
      wrapper.classList.add("pending");
      const pendingText = message.status || "Preparing request...";
      wrapper.innerHTML = `
        <div class="message-role">DIAL</div>
        <div class="message-content pending-content" aria-label="Request in progress">
          <span class="pending-text">${escapeHtml(pendingText)}</span>
          <span class="pending-dot"></span><span class="pending-dot"></span><span class="pending-dot"></span>
        </div>
      `;
    } else {
      wrapper.innerHTML = `
        <div class="message-role">${role === "user" ? "You" : role === "error" ? "Error" : "DIAL"}</div>
        <div class="message-content">${markdownToHtml(message.content)}</div>
      `;
      if (role === "assistant" && message.routing_trace) {
        const label = `Assistant message ${assistantIndex}`;
        const traceId = `${state.currentConversationId || "conversation"}-assistant-${assistantIndex}`;
        traces.push({ id: traceId, trace: message.routing_trace, label });
        wrapper.appendChild(makeTraceButton(traceId, message.routing_trace, label));
      }
    }
    els.messages.appendChild(wrapper);
  }

  const selected = traces.find((item) => item.id === state.selectedTraceId);
  if (selected) {
    state.selectedTrace = selected.trace;
    state.selectedTraceLabel = selected.label;
  } else {
    resetTraceSelection();
  }

  restoreMessagesScroll(scrollState);
  renderTraceInspector();
  syncTraceButtons();
  typesetMath(scrollState);
}

function formatContextMeter(info) {
  if (!info || typeof info !== "object") {
    return "";
  }
  const tokens = Number(info.chat_approx_tokens ?? info.approx_tokens ?? 0);
  const maxTokens = Number(info.max_context_tokens || 0);
  if (!maxTokens) {
    return "";
  }
  const used = Math.max(0, Math.min(tokens, maxTokens));
  const includedMessages = Math.max(0, Number(info.included_messages || 0));
  const availableMessages = Math.max(0, Number(info.available_messages || 0));
  const currentMessage = info.current_message_included ? 1 : 0;
  const sentMessages = includedMessages + currentMessage;
  const totalMessages = availableMessages + currentMessage;
  const messageText =
    totalMessages > 0
      ? `${sentMessages.toLocaleString()} / ${totalMessages.toLocaleString()} messages`
      : "no message yet";
  const note = info.truncated ? " Older messages stay in chat but are not sent." : "";
  return (
    `Sent as context: ${messageText} ` +
    `(~${used.toLocaleString()} / ${maxTokens.toLocaleString()} tokens).${note}`
  );
}

function renderContextMeter(info = null) {
  if (!els.contextMeter) {
    return;
  }
  const text = formatContextMeter(info);
  els.contextMeter.textContent = text || "";
}

async function refreshContextPreview() {
  if (!els.contextMeter) {
    return;
  }
  const sequence = ++state.contextPreviewSeq;
  const previewMessages = (state.currentConversation?.messages || [])
    .filter((message) => !message.pending)
    .map((message) => ({
      role: message.role,
      content: message.content,
    }));
  const draft = els.prompt.value.trim();
  try {
    const data = await api("/api/context/preview", {
      method: "POST",
      body: JSON.stringify({
        conversation_id: state.currentConversationId?.startsWith("local-")
          ? null
          : state.currentConversationId,
        messages: previewMessages,
        prompt: draft,
      }),
    });
    if (sequence !== state.contextPreviewSeq) {
      return;
    }
    renderContextMeter(data.context_info);
  } catch {
    if (sequence === state.contextPreviewSeq) {
      renderContextMeter(null);
    }
  }
}

function scheduleContextPreview(delay = 180) {
  if (state.contextPreviewTimer) {
    clearTimeout(state.contextPreviewTimer);
  }
  state.contextPreviewTimer = setTimeout(refreshContextPreview, delay);
}

function typesetMath(scrollState = messagesScrollState()) {
  if (window.MathJax?.typesetPromise) {
    window.MathJax.typesetPromise([els.messages])
      .then(() => restoreMessagesScroll(scrollState))
      .catch(() => {});
  }
}

function makeTitle(text) {
  const compact = text.trim().replace(/\s+/g, " ");
  if (!compact) {
    return "Untitled chat";
  }
  return compact.length <= 34 ? compact : `${compact.slice(0, 31).trim()}...`;
}

function ensureOptimisticConversation(prompt) {
  if (state.currentConversation) {
    return {
      conversation: state.currentConversation,
      serverConversationId: state.currentConversationId?.startsWith("local-")
        ? null
        : state.currentConversationId,
    };
  }

  const conversation = {
    id: `local-${Date.now()}`,
    title: makeTitle(prompt),
    messages: [],
    updated_at: Date.now() / 1000,
  };
  state.currentConversation = conversation;
  state.currentConversationId = conversation.id;
  state.conversations = [conversation, ...state.conversations];
  return { conversation, serverConversationId: null };
}

async function refreshState() {
  clearAppAlert();
  const data = await api("/api/state");
  state.nodes = data.nodes;
  state.conversations = data.conversations;
  renderNodes();
  renderConversations();
  scheduleContextPreview(0);
}

async function selectConversation(conversationId) {
  clearAppAlert();
  const conversation = await api(`/api/conversations/${encodeURIComponent(conversationId)}`);
  state.currentConversationId = conversation.id;
  state.currentConversation = conversation;
  resetTraceSelection();
  renderConversations();
  renderMessages();
  scheduleContextPreview(0);
}

function newChat() {
  clearAppAlert();
  state.currentConversationId = null;
  state.currentConversation = null;
  resetTraceSelection();
  renderConversations();
  renderMessages();
  scheduleContextPreview(0);
  els.prompt.focus();
}

async function deleteChat() {
  if (!state.currentConversationId) {
    return;
  }
  clearAppAlert();
  const data = await api(`/api/conversations/${encodeURIComponent(state.currentConversationId)}`, {
    method: "DELETE",
  });
  state.conversations = data.conversations;
  state.currentConversationId = null;
  state.currentConversation = null;
  resetTraceSelection();
  renderConversations();
  renderMessages();
  scheduleContextPreview(0);
}

async function sendMessage(event) {
  event.preventDefault();
  clearAppAlert();
  const prompt = els.prompt.value.trim();
  const entryNode = els.nodeSelect.value;
  if (!prompt || !entryNode) {
    return;
  }

  const { conversation, serverConversationId } = ensureOptimisticConversation(prompt);
  conversation.messages.push({
    role: "User",
    content: prompt,
    created_at: Date.now() / 1000,
  });
  conversation.messages.push({
    role: "Assistant",
    content: "",
    pending: true,
    created_at: Date.now() / 1000,
  });
  conversation.updated_at = Date.now() / 1000;
  renderConversations();
  renderMessages();

  els.prompt.value = "";
  scheduleContextPreview(0);
  els.send.disabled = true;

  try {
    const start = await api("/api/chat/start", {
      method: "POST",
      body: JSON.stringify({
        conversation_id: serverConversationId,
        entry_node: entryNode,
        prompt,
      }),
    });

    state.currentConversationId = start.conversation.id;
    conversation.id = start.conversation.id;
    state.currentConversation = conversation;
    state.conversations = start.conversations;
    const pending = conversation.messages.find((message) => message.pending);
    if (pending) {
      pending.context_info = start.context_info;
    }
    renderConversations();
    renderMessages();

    while (true) {
      await sleep(1000);
      const status = await api(`/api/chat/status/${encodeURIComponent(start.query_id)}`);
      const pending = conversation.messages.find((message) => message.pending);
      if (pending && status.message) {
        pending.status = status.message;
        renderMessages();
      }

      if (!status.done) {
        continue;
      }

      const data = status.response;
      if (!data?.ok) {
        throw new Error(data?.error || data?.answer || "The query failed.");
      }

      state.currentConversation = data.conversation;
      state.currentConversationId = data.conversation.id;
      state.conversations = data.conversations;
      renderConversations();
      renderMessages();
      scheduleContextPreview(0);
      break;
    }
  } catch (error) {
    const pendingIndex = conversation.messages.findIndex((message) => message.pending);
    if (pendingIndex >= 0) {
      conversation.messages.splice(pendingIndex, 1);
    }
    showAppAlert(error.message || "The request failed.", "error");
    renderMessages();
    scheduleContextPreview(0);
  } finally {
    els.send.disabled = false;
    els.prompt.focus();
  }
}

els.form.addEventListener("submit", sendMessage);
els.refresh.addEventListener("click", () => {
  refreshState().catch((error) => {
    showAppAlert(error.message || "Could not refresh the app state.", "error");
  });
});
els.newChat.addEventListener("click", newChat);
els.deleteChat.addEventListener("click", () => {
  deleteChat().catch((error) => {
    showAppAlert(error.message || "Could not delete the conversation.", "error");
  });
});
els.inspectorResize.addEventListener("pointerdown", startInspectorResize);
els.inspectorResize.addEventListener("pointermove", (event) => {
  if (els.inspectorResize.hasPointerCapture(event.pointerId)) {
    resizeInspector(event);
  }
});
els.inspectorResize.addEventListener("pointerup", stopInspectorResize);
els.inspectorResize.addEventListener("pointercancel", stopInspectorResize);
els.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    els.form.requestSubmit();
  }
});
els.prompt.addEventListener("input", () => scheduleContextPreview());

refreshState()
  .then(() => {
    renderMessages();
    scheduleContextPreview(0);
  })
  .catch((error) => {
    showAppAlert(error.message || "Could not load the app state.", "error");
  });
