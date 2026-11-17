/* Silver pencil on black paper. Classic scripts expose a pure offline test API. */
(function (root, factory) {
  "use strict";
  var art = typeof module === "object" && module.exports ? require("./intro_art.js") : root && root.IgrisIntroArt;
  var api = factory(art);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root && root.document) { root.IgrisIntro = api; api.startIntro(root); }
})(typeof window === "undefined" ? null : window, function (art) {
  "use strict";
  // A missing art asset must still reach the fail-open lifecycle below.
  var TIMELINE = art ? art.TIMELINE : { reveal: 14, end: 14.8 };
  var OPACITIES = [0.04, 0.14, 0.3, 0.5, 0.72, 0.95];
  function clamp(n) { return Math.max(0, Math.min(1, n)); }
  function reviewTime(hash) {
    var match = /^#t=(\d+(?:\.\d+)?)$/.exec(hash);
    return match && Number.isFinite(Number(match[1])) ? Number(match[1]) : null;
  }
  function lowQuality(width, concurrency) { return width < 600 || (concurrency > 0 && concurrency <= 4); }
  function phaseAt(t) {
    if (t < TIMELINE.gesture) return "cluster";
    if (t < TIMELINE.contour) return "gesture";
    if (t < TIMELINE.sweep) return "contour";
    if (t < TIMELINE.hold) return "sweep";
    if (t < TIMELINE.drop) return "hold";
    if (t < TIMELINE.burn) return "drop";
    if (t < TIMELINE.ash) return "burn";
    if (t < TIMELINE.wordEnd) return "ash";
    if (t < TIMELINE.reveal) return "word";
    return t < TIMELINE.end ? "reveal" : "done";
  }

  function layout(width, height) {
    // The paper has generous empty side margins; fit its occupied region on phones.
    var h = Math.min(height * 1.06, width * 2.05), w = h * 2 / 3;
    return { x: (width - w) / 2, y: (height - h) / 2, width: w, height: h };
  }
  function createRenderer(ctx, rose, makeCanvas) {
    var strokes = rose.strokes, random = art.randomSource(rose.seed ^ 0x1f3a), count = 0, i, j;
    var offsets = new Uint32Array(strokes.length + 1);
    for (i = 0; i < strokes.length; i++) { offsets[i] = count; count += (strokes[i].points.length / 2 - 1) * 2; }
    offsets[strokes.length] = count;
    // Two precomputed graphite sub-passes. All hot-loop buffers are reused.
    var x = new Float32Array(count), y = new Float32Array(count), ex = new Float32Array(count), ey = new Float32Array(count);
    var force = new Float32Array(count), alpha = new Float32Array(count), burn = new Float32Array(count);
    var particles = null, paperBox = null, particle = new Float64Array(4), drop = new Float64Array(4);
    var next = new Int32Array(count), heads = new Int32Array(7 * 6 * 3);
    var dx = new Float32Array(count), dy = new Float32Array(count), dex = new Float32Array(count), dey = new Float32Array(count);
    var colors = ["#d9dcec", "#fff0d9", "#ffbf55", "#ff6a2a", "#7a1c0c", "#8a8fa6", "#ffad59"];
    var index = 0, completionOrder = rose.schedule.completionOrder;
    var prefix = new Float64Array(4), tip = new Float64Array(5), tail = new Float64Array(5);
    for (i = 0; i < strokes.length; i++) {
      var s = strokes[i], p = s.points, n = p.length / 2 - 1;
      for (j = 0; j < n; j++) {
        for (var pass = 0; pass < 2; pass++) {
          var jitterX = (random() - 0.5) / 700, jitterY = (random() - 0.5) / 1050;
          x[index] = p[j*2]+jitterX; y[index] = p[j*2+1]+jitterY;
          ex[index] = p[j*2+2]+jitterX; ey[index] = p[j*2+3]+jitterY;
          force[index] = (s.force[j] + s.force[j+1]) / 2;
          alpha[index] = force[index] * s.grain[j] * (pass ? 0.43 : 0.8);
          burn[index] = art.burnTime((p[j*2]+p[j*2+2])/2, (p[j*2+1]+p[j*2+3])/2);
          index++;
        }
      }
    }
    var layers = [makeCanvas(), makeCanvas(), makeCanvas()];
    var targets = [layers[0].getContext("2d"), layers[1].getContext("2d"), layers[2].getContext("2d")];
    if (!targets[0] || !targets[1] || !targets[2]) throw Error("Offscreen canvas unavailable");
    var cachedWidth = 0, cachedHeight = 0, cachedDpr = 0, completed = 0;
    var left = 0, top = 0, w = 1, h = 1, previousTime = -1;
    function resetLayers() {
      for (var layer = 0; layer < 3; layer++) {
        layers[layer].width = Math.round(cachedWidth * cachedDpr); layers[layer].height = Math.round(cachedHeight * cachedDpr);
        targets[layer].setTransform(cachedDpr, 0, 0, cachedDpr, 0, 0); targets[layer].lineCap = "round";
      }
      var grain = art.randomSource(rose.seed ^ 0x784b), paper = targets[2];
      paper.fillStyle = "#02030a"; paper.globalAlpha = 1; paper.fillRect(0, 0, cachedWidth, cachedHeight);
      paper.strokeStyle = "#d9dcec"; paper.globalAlpha = 0.025; paper.lineWidth = 0.55; paper.beginPath();
      for (var k = 0; k < 1800; k++) {
        var gx = grain() * cachedWidth, gy = grain() * cachedHeight;
        paper.moveTo(gx, gy); paper.lineTo(gx + 0.4 + grain() * 0.7, gy + grain() * 0.4);
      }
      paper.stroke(); paper.globalAlpha = 1; completed = 0;
    }
    function queue(k, color, opacity, pressure) {
      if (opacity < 0.012) return;
      var a = opacity < 0.045 ? 0 : opacity < 0.12 ? 1 : opacity < 0.24 ? 2 : opacity < 0.4 ? 3 : opacity < 0.6 ? 4 : 5;
      var weight = Math.min(2, Math.floor(pressure * 3)), bucket = color * 18 + a * 3 + weight;
      next[k] = heads[bucket]; heads[bucket] = k;
    }
    function flush(target) {
      for (var bucket = 0; bucket < heads.length; bucket++) {
        if (heads[bucket] < 0) continue;
        target.strokeStyle = colors[Math.floor(bucket / 18)];
        target.globalAlpha = OPACITIES[Math.floor(bucket % 18 / 3)]; target.lineWidth = 0.6 + (bucket % 3) * 0.6;
        target.beginPath();
        for (var k = heads[bucket]; k >= 0; k = next[k]) { target.moveTo(dx[k], dy[k]); target.lineTo(dex[k], dey[k]); }
        target.stroke();
      }
      target.globalAlpha = 1;
    }
    function position(k) { dx[k] = left+x[k]*w; dy[k] = top+y[k]*h; dex[k] = left+ex[k]*w; dey[k] = top+ey[k]*h; }
    function drawStroke(target, strokeIndex, u) {
      heads.fill(-1);
      art.partialReveal(strokes[strokeIndex],u,prefix);
      var start = offsets[strokeIndex], end = offsets[strokeIndex+1];
      var limit = Math.min(end,start+(prefix[2]+1)*2);
      for (var k = start; k < limit; k++) {
        position(k);
        var fraction = Math.floor((k-start)/2) === prefix[2] ? prefix[3] : 1;
        if (fraction <= 0) continue;
        dex[k] = dx[k]+(dex[k]-dx[k])*fraction; dey[k] = dy[k]+(dey[k]-dy[k])*fraction;
        queue(k, 0, alpha[k], force[k]);
      }
      flush(target);
    }
    function penTip(penId, t) {
      art.samplePen(rose,penId,t,tip);
      var px = left+tip[0]*w, py = top+tip[1]*h, opacity = tip[2], tailLength = 0;
      if (opacity <= 0) return;
      ctx.strokeStyle = "#d9dcec"; ctx.lineWidth = 0.85;
      // Fixed time samples, rather than frame history, keep frozen tails exact.
      for (var trail = 1; trail <= 5; trail++) {
        art.samplePen(rose,penId,t-trail*0.006,tail);
        var tx = left+tail[0]*w, ty = top+tail[1]*h;
        var span = Math.hypot(tx-px,ty-py), remaining = 14-tailLength;
        if (span > remaining) { tx = px+(tx-px)*remaining/span; ty = py+(ty-py)*remaining/span; }
        ctx.globalAlpha = opacity*(1-trail/6)*0.55; ctx.beginPath();
        ctx.moveTo(px,py); ctx.lineTo(tx,ty); ctx.stroke(); px = tx; py = ty;
        tailLength += span;
        if (tailLength >= 14) break;
      }
      px = left+tip[0]*w; py = top+tip[1]*h;
      // Concentric low-alpha discs avoid expensive per-frame shadow filters.
      ctx.fillStyle = "#edf0fa";
      for (var glow = 3; glow >= 1; glow--) {
        ctx.globalAlpha = opacity*(glow === 1 ? 1 : glow === 2 ? 0.12 : 0.035);
        ctx.beginPath(); ctx.arc(px,py,glow === 1 ? 1.1 : glow*1.7,0,Math.PI*2); ctx.fill();
      }
      var tick = Math.floor(t*17+penId*2.7);
      if (tip[3] >= 0 && (tick+penId)%7 === 0) {
        var angle = tick*2.4+penId, age = (t*17+penId*2.7)-tick;
        var qx = px+Math.cos(angle)*(2+age*4), qy = py+Math.sin(angle)*(2+age*4);
        ctx.globalAlpha = opacity*(1-age)*0.35; ctx.beginPath();
        ctx.moveTo(qx,qy); ctx.lineTo(qx+0.5,qy-0.7); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    function burning(t) {
      heads.fill(-1);
      for (var k = 0; k < count; k++) {
        var age = t-burn[k], color = 0, opacity = alpha[k], pressure = force[k]; position(k);
        if (age >= 0) {
          color = age < 0.075 ? 1 : age < 0.17 ? 2 : age < 0.29 ? 3 : 4;
          opacity = Math.max(opacity,0.48)*(age < 0.29 ? 1 : 0.7);
          var ashAt = Math.max(TIMELINE.ash,burn[k]+0.34);
          opacity *= 1-clamp((t-ashAt)/0.16);
        }
        queue(k,color,opacity,pressure);
      }
      flush(ctx);
    }
    function completionSweep(t) {
      var route = rose.schedule, progress = clamp((t-TIMELINE.sweep)/route.sweepDuration);
      var bandY = top+(route.sweepTop+(route.sweepBottom-route.sweepTop)*progress)*h;
      var fade = Math.min(1,(t-TIMELINE.sweep)/0.12)*Math.min(1,(TIMELINE.hold-t)/0.2);
      // Layered translucent strips give a soft band without filters or frame allocations.
      ctx.fillStyle = "#d9dcec";
      for (var strip = 12; strip >= 1; strip--) {
        ctx.globalAlpha = 0.006*fade;
        ctx.fillRect(left+w*0.14,bandY-strip*1.5,w*0.72,strip*3);
      }
      ctx.globalAlpha = 1;
    }
    function fallingDrop(t) {
      if (t < TIMELINE.drop || t > TIMELINE.burn+0.45) return;
      art.sampleDrop(t,paperBox,drop);
      var px = drop[0], py = drop[1];
      ctx.strokeStyle = "#d9dcec"; ctx.lineWidth = 0.8;
      if (drop[2]) {
        ctx.globalAlpha = 0.75; ctx.beginPath();
        ctx.moveTo(px,py-drop[3]); ctx.lineTo(px-2.4,py); ctx.lineTo(px-1.5,py+2.5);
        ctx.lineTo(px+1.5,py+2.5); ctx.lineTo(px+2.4,py); ctx.lineTo(px,py-drop[3]); ctx.stroke();
        ctx.globalAlpha = 0.4; ctx.beginPath(); ctx.moveTo(px-0.6,py-3); ctx.lineTo(px-1,py); ctx.stroke();
      } else {
        var age = (t-TIMELINE.burn)/0.45;
        ctx.globalAlpha = (1-age)*0.65; ctx.beginPath();
        // One shallow expanding ring and six ballistic silver droplets.
        for (var j = 0; j <= 32; j++) {
          var angle = j/32*Math.PI*2, rx = px+Math.cos(angle)*(2+age*22), ry = py+Math.sin(angle)*(1+age*7);
          if (j) ctx.lineTo(rx,ry); else ctx.moveTo(rx,ry);
        }
        ctx.stroke(); ctx.beginPath();
        for (j = 0; j < 6; j++) {
          var vx = (j-2.5)*20, vy = -35-(j%3)*12;
          var sx = px+vx*age, sy = py+vy*age+35*age*age;
          ctx.moveTo(sx,sy); ctx.lineTo(sx+0.7,sy+1.2);
        }
        ctx.stroke();
        if (t-TIMELINE.burn < 0.075) {
          ctx.fillStyle = "#fff0d9"; ctx.globalAlpha = 1-age;
          ctx.beginPath(); ctx.arc(px,py,2.2,0,Math.PI*2); ctx.fill();
        }
      }
      ctx.globalAlpha = 1;
    }
    function drawAsh(t) {
      // Three batches keep thousands of particles to three strokes per frame.
      for (var bucket = 0; bucket < 3; bucket++) {
        ctx.strokeStyle = bucket === 0 ? "#8a8fa6" : bucket === 1 ? "#ffad59" : "#d9dcec";
        ctx.lineWidth = bucket === 0 ? 0.7 : 1.1;
        ctx.globalAlpha = bucket === 0 ? 0.35 : bucket === 1 ? 0.48 : 0.7;
        ctx.beginPath();
        for (var j = 0; j < particles.particles.length; j++) {
          var p = particles.particles[j];
          art.sampleParticle(p,t,particle);
          if (particle[2] <= 0) continue;
          var color = !p.word ? 0 : particle[3] > 0.25 ? 1 : 2;
          if (color !== bucket) continue;
          ctx.moveTo(particle[0],particle[1]);
          ctx.lineTo(particle[0]+1.1*particle[2],particle[1]-0.4*particle[2]);
        }
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    return function render(t, width, height, dpr) {
      dpr = Math.max(1,Math.min(2,dpr || 1));
      if (width !== cachedWidth || height !== cachedHeight || dpr !== cachedDpr) {
        cachedWidth = width; cachedHeight = height; cachedDpr = dpr;
        var box = layout(width,height); left = box.x; top = box.y; w = box.width; h = box.height;
        paperBox = box; particles = art.buildParticles(rose,width,height,box); resetLayers();
      } else if (t < previousTime) resetLayers();
      previousTime = t;
      // Preserve the same compositing order for direct seeks and incremental playback.
      while (completed < strokes.length && strokes[completionOrder[completed]].end <= t) {
        var id = completionOrder[completed++];
        drawStroke(targets[strokes[id].part],id,1);
      }
      ctx.setTransform(dpr,0,0,dpr,0,0); ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1; ctx.lineCap = "round";
      ctx.drawImage(layers[2],0,0,width,height);
      if (t < TIMELINE.burn) {
        ctx.drawImage(layers[0],0,0,width,height); ctx.drawImage(layers[1],0,0,width,height);
      } else if (t < TIMELINE.burnEnd+0.5) burning(t);
      if (t < TIMELINE.sweep) {
        for (var pen = 0; pen < rose.schedule.penCount; pen++) {
          art.samplePen(rose,pen,t,tip);
          if (tip[3] >= 0 && strokes[tip[3]].end > t) drawStroke(ctx,tip[3],tip[4]);
        }
      } else if (t < TIMELINE.hold) {
        for (var si = 0; si < strokes.length; si++) {
          var mark = strokes[si];
          if (mark.pen === -1 && mark.start <= t && t < mark.end)
            drawStroke(ctx,si,clamp((t-mark.start)/(mark.end-mark.start)));
        }
        completionSweep(t);
      }
      if (t < TIMELINE.sweep+0.6) for (pen = 0; pen < rose.schedule.penCount; pen++) penTip(pen,t);
      fallingDrop(t);
      if (t >= TIMELINE.ash) drawAsh(t);
      ctx.globalAlpha = 1;
    };
  }

  function startIntro(win) {
    var doc = win.document, html = doc.documentElement;
    var canvas = null, card = null, fields = [], tabIndices = [];
    var raf = null, safety = null, revealTimer = null, media = null;
    var done = false, revealing = false, quickReveal = false, review = false, render = null;
    var width = 0, height = 0, dpr = 1, started = null, stillTime = 0;
    var skipEvents = ["pointerdown", "touchstart", "click", "keydown"];

    function removeListeners() {
      for (var i = 0; i < skipEvents.length; i++) doc.removeEventListener(skipEvents[i], skip, true);
      doc.removeEventListener("DOMContentLoaded", ready);
      win.removeEventListener("resize", resize);
      win.removeEventListener("hashchange", seekReview);
      if (media && media.removeEventListener) media.removeEventListener("change", motionChanged);
    }
    function finish() {
      if (done) return;
      done = true;
      if (raf !== null && win.cancelAnimationFrame) win.cancelAnimationFrame(raf);
      win.clearTimeout(safety); win.clearTimeout(revealTimer);
      removeListeners();
      if (canvas) canvas.remove();
      html.classList.remove("intro-active", "intro-revealing", "intro-fast");
      card = card || doc.querySelector(".login-card");
      if (card) {
        card.inert = false; card.removeAttribute("inert"); card.removeAttribute("aria-hidden");
        for (var i = 0; i < fields.length; i++) {
          if (tabIndices[i] === null) fields[i].removeAttribute("tabindex");
          else fields[i].setAttribute("tabindex", tabIndices[i]);
        }
        var username = doc.getElementById("username");
        if (username) username.focus({ preventScroll: true });
      }
    }
    function reveal(quick) {
      if (done || (revealing && (!quick || quickReveal))) return;
      revealing = true;
      if (quick) {
        quickReveal = true;
        if (raf !== null) win.cancelAnimationFrame(raf);
        html.classList.add("intro-fast");
      }
      win.clearTimeout(revealTimer);
      html.classList.add("intro-revealing");
      revealTimer = win.setTimeout(finish, quick ? 200 : 800);
    }
    function skip(event) {
      if (done || review) return;
      // Capture before focus moves, including the key that initiated the skip.
      event.preventDefault(); event.stopImmediatePropagation();
      try { reveal(true); } catch (_) { finish(); }
    }
    function motionChanged(event) { if (event.matches) finish(); }
    function seekReview() {
      if (done || !review) return;
      try {
        var time = reviewTime(win.location.hash);
        if (time === null) { finish(); return; }
        stillTime = time;
        render(stillTime, width, height, dpr);
      } catch (_) { finish(); }
    }
    function resize() {
      if (done) return;
      try {
        width = Math.max(1, win.innerWidth); height = Math.max(1, win.innerHeight);
        dpr = Math.min(2, win.devicePixelRatio || 1);
        canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
        if (review) render(stillTime, width, height, dpr);
      } catch (_) { finish(); }
    }
    function frame(timestamp) {
      if (done) return;
      try {
        if (started === null) started = timestamp;
        var t = (timestamp - started) / 1000;
        render(t, width, height, dpr);
        if (t >= TIMELINE.end) { finish(); return; }
        if (t >= TIMELINE.reveal) reveal(false);
        raf = win.requestAnimationFrame(frame);
      } catch (_) { finish(); }
    }
    function ready() {
      if (done) return;
      try {
        card = doc.querySelector(".login-card");
        if (!card) { finish(); return; }
        card.inert = true; card.setAttribute("inert", ""); card.setAttribute("aria-hidden", "true");
        fields = card.querySelectorAll("input, button, select, textarea, a[href], [tabindex]");
        for (var i = 0; i < fields.length; i++) {
          tabIndices[i] = fields[i].getAttribute("tabindex"); fields[i].setAttribute("tabindex", "-1");
        }
        if (doc.activeElement && card.contains(doc.activeElement)) doc.activeElement.blur();
        canvas = doc.createElement("canvas"); canvas.className = "intro-canvas";
        canvas.setAttribute("aria-hidden", "true");
        var ctx = canvas.getContext && canvas.getContext("2d");
        if (!ctx) { finish(); return; }
        doc.body.appendChild(canvas);
        var time = reviewTime(win.location.hash);
        review = time !== null;
        stillTime = review ? time : 0;
        var seed = review ? 137508 : Math.floor(Math.random() * 4294967296);
        var low = lowQuality(win.innerWidth, win.navigator && win.navigator.hardwareConcurrency);
        render = createRenderer(ctx, art.buildRose(seed, low), function () { return doc.createElement("canvas"); });
        resize();
        if (done) return;
        win.addEventListener("resize", resize);
        if (review) {
          // Deterministic review frames are the sole exception to the watchdog.
          win.clearTimeout(safety);
          win.addEventListener("hashchange", seekReview);
        } else {
          for (i = 0; i < skipEvents.length; i++) doc.addEventListener(skipEvents[i], skip, { capture: true, passive: false });
          raf = win.requestAnimationFrame(frame);
        }
      } catch (_) { finish(); }
    }
    try {
      safety = win.setTimeout(finish, 15200);
      media = win.matchMedia ? win.matchMedia("(prefers-reduced-motion: reduce)") : null;
      if ((media && media.matches) || typeof win.requestAnimationFrame !== "function" ||
          typeof win.cancelAnimationFrame !== "function") { finish(); return; }
      if (media && media.addEventListener) media.addEventListener("change", motionChanged);
      html.classList.add("intro-active");
      if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", ready, { once: true });
      else ready();
    } catch (_) { finish(); }
  }

  return { buildRose: art && art.buildRose, createRenderer: createRenderer, startIntro: startIntro,
    PEN_COUNT: art && art.PEN_COUNT, partialReveal: art && art.partialReveal, samplePen: art && art.samplePen,
    phaseAt: phaseAt, reviewTime: reviewTime, lowQuality: lowQuality, layout: layout, TIMELINE: TIMELINE };
});
