/* Original pencil study. Coordinates are authored on a 600 by 900 sheet, then normalized.
 * Each visible petal is its own unequal pair of cubic edges, not a radial template.
 * No image is read by this module. The two edges also bound the turning hatch marks. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.IgrisIntroArt = api;
})(typeof window === "undefined" ? null : window, function () {
  "use strict";
  var TIMELINE = Object.freeze({ cluster: 0, gesture: 0.3, contour: 1.2, sweep: 6,
    hold: 8.2, drop: 8.4, burn: 9.3, ash: 10.6, wordStart: 11.4, burnEnd: 11.6,
    wordEnd: 13.6, reveal: 14, cardEnd: 14.8, end: 14.8 });
  var PEN_COUNT = Object.freeze({ desktop: 12, reduced: 8 });
  var TRAVEL_RATIO = 12;
  var HEART = Object.freeze([307 / 600, 270 / 900]);
  var IGNITION = Object.freeze([322 / 600, 193 / 900]);
  function randomSource(seed) {
    var state = seed >>> 0;
    return function () {
      state = (state + 0x6d2b79f5) | 0;
      var n = Math.imul(state ^ (state >>> 15), 1 | state);
      n ^= n + Math.imul(n ^ (n >>> 7), 61 | n);
      return ((n ^ (n >>> 14)) >>> 0) / 4294967296;
    };
  }
  function cubic(c, t) {
    var s = 1 - t;
    return [s * s * s * c[0] + 3 * s * s * t * c[2] + 3 * s * t * t * c[4] + t * t * t * c[6],
      s * s * s * c[1] + 3 * s * s * t * c[3] + 3 * s * t * t * c[5] + t * t * t * c[7]];
  }
  function curve(c, steps) {
    var points = [];
    for (var i = 0; i <= (steps || 40); i++) points.push(cubic(c, i / (steps || 40)));
    return points;
  }
  // Ordered inside out: pinched heart, winding cup, side folds, open outer petals.
  // The last number controls how much of the lit face receives silver hatching.
  var PETALS = [
    [[263,249, 277,235, 332,226, 333,263], [263,249, 292,246, 321,241, 333,263], 0.65],
    [[333,263, 353,271, 329,307, 299,303], [333,263, 344,288, 319,298, 299,303], 0.35],
    [[263,249, 260,251, 259,292, 299,303], [263,249, 239,249, 250,310, 299,303], 0.55],
    [[263,249, 246,204, 348,200, 354,263], [263,249, 266,217, 339,218, 354,263], 0.8],
    [[354,263, 385,270, 356,337, 292,343], [354,263, 367,304, 328,328, 292,343], 0.45],
    [[263,249, 216,228, 210,319, 292,343], [263,249, 192,213, 184,337, 292,343], 0.68],
    [[215,277, 197,226, 211,176, 282,190], [215,277, 175,212, 194,158, 282,190], 0.5],
    [[215,277, 249,180, 335,175, 374,229], [215,277, 267,153, 356,165, 374,229], 0.8],
    [[282,190, 298,139, 368,145, 374,229], [282,190, 326,210, 365,220, 374,229], 0.7],
    [[374,229, 419,164, 467,205, 365,329], [374,229, 421,228, 408,303, 365,329], 0.65],
    [[215,277, 203,284, 200,317, 224,350], [215,277, 150,256, 162,356, 224,350], 0.72],
    [[224,350, 264,374, 335,352, 365,329], [224,350, 261,419, 351,391, 365,329], 0.9],
    [[365,329, 436,301, 491,284, 417,390], [365,329, 430,346, 451,368, 417,390], 0.72],
    [[256,387, 318,410, 374,374, 417,390], [256,387, 327,467, 401,440, 417,390], 0.76],
    [[215,277, 168,276, 137,316, 112,330], [215,277, 121,281, 108,319, 112,330], 0.6],
    [[112,330, 150,391, 204,410, 256,387], [112,330, 153,457, 281,485, 256,387], 0.78]
  ];

  // Handwritten dotted-I Igris curves are ash targets only, never drawing strokes.
  var SIGNATURE = [[389,770, 403,764, 418,762, 418,765], [411,763, 405,782, 399,796, 396,800],
      [387,800, 397,796, 408,794, 410,796], [412,753, 413,752, 414,751, 414,750],
      [432,781, 417,774, 412,798, 423,793], [423,793, 435,777, 432,777, 429,790],
      [429,790, 418,829, 403,810, 429,801], [429,801, 440,791, 440,778, 442,780],
      [442,780, 442,794, 439,794, 443,788], [443,788, 452,774, 456,781, 451,784],
      [451,791, 456,779, 457,779, 455,788], [455,788, 450,799, 460,791, 464,787],
      [458,773, 459,771, 460,771, 460,770], [477,780, 468,773, 462,785, 470,786],
      [470,786, 483,795, 458,800, 465,791], [386,811, 416,806, 466,803, 487,796]
    ];

  function buildRose(seed, low) {
    var random = randomSource(seed), strokes = [], serial = 0;
    function add(points, kind, part, pressure, weight, label) {
      var resampled = [], total = 0, j;
      // Arc-length resampling keeps graphite grain independent of authoring knots.
      for (j = 1; j < points.length; j++) {
        var a = points[j - 1], b = points[j], length = Math.hypot(b[0] - a[0], b[1] - a[1]);
        var steps = Math.max(1, Math.ceil(length / 2.5));
        for (var k = 0; k < steps; k++) resampled.push([a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps]);
        total += length;
      }
      resampled.push(points[points.length - 1]);
      var xy = new Float32Array(resampled.length * 2), force = new Float32Array(resampled.length);
      var grain = new Float32Array(resampled.length), phase = random() * 6.28;
      for (j = 0; j < resampled.length; j++) {
        var u = j / (resampled.length - 1), taper = Math.pow(Math.sin(Math.PI * u), 0.38);
        var wobble = Math.sin(j * 0.71 + phase) * 0.3 + Math.sin(j * 0.19 + phase) * 0.45;
        xy[j * 2] = (resampled[j][0] + wobble + (random() - 0.5) * 0.32) / 600;
        xy[j * 2 + 1] = (resampled[j][1] + wobble * 0.7 + (random() - 0.5) * 0.32) / 900;
        force[j] = pressure * (0.18 + 0.82 * taper) * (0.8 + 0.2 * Math.sin(u * 9 + phase));
        grain[j] = random() < 0.13 ? 0.12 : 0.48 + random() * 0.52;
      }
      strokes.push({ id: serial++, kind: kind, part: part, points: xy, pressure: pressure,
        force: force, grain: grain, weight: weight || Math.pow(total, 0.55), length: total / 900,
        label: label || "", start: 0, end: 0, moveStart: 0 });
    }
    function pen(c, kind, part, pressure, label) { add(curve(c), kind, part, pressure, 0, label); }
    function oval(cx, cy, rx, ry, tilt, kind, part, pressure, label) {
      var points = [];
      for (var k = 0; k <= 28; k++) {
        var a = k / 28 * Math.PI * 2, x = rx * Math.cos(a), y = ry * Math.sin(a);
        points.push([cx + x * Math.cos(tilt) - y * Math.sin(tilt), cy + x * Math.sin(tilt) + y * Math.cos(tilt)]);
      }
      add(points, kind, part, pressure, 0, label);
    }
    // Searching knot, open cup and long gesture. These are retained beneath the study.
    pen([307,272, 273,230, 344,240, 302,287], "gesture", 1, 0.2);
    pen([302,287, 274,273, 321,235, 316,268], "gesture", 1, 0.17);
    pen([185,311, 140,99, 423,80, 426,303], "gesture", 1, 0.12);
    pen([161,309, 186,485, 404,489, 441,302], "gesture", 1, 0.13);
    pen([303,420, 326,535, 267,694, 299,818], "gesture", 0, 0.18);
    oval(204,574, 106,33, 0.51, "gesture", 0, 0.10);
    oval(390,611, 94,31, -0.35, "gesture", 0, 0.11);
    oval(229,749, 64,23, -0.9, "gesture", 0, 0.08);

    pen([307,270, 296,250, 321,246, 319,265], "contour", 1, 0.72, "heart");
    pen([319,265, 317,284, 293,285, 291,268], "contour", 1, 0.65, "heart");
    PETALS.forEach(function (p, id) {
      pen(p[0], "contour", 1, 0.67 + id % 3 * 0.07, "petal-" + id);
      pen(p[1], "contour", 1, 0.37 + id % 4 * 0.055, "fold-" + id);
      // Partial restatements and rolled lips: never a duplicate of a full outline.
      if (id % 3 !== 1) {
        var points = [];
        for (var k = 4; k <= 25; k++) {
          var a = cubic(p[0], k / 32), b = cubic(p[1], k / 32);
          points.push([a[0] * 0.94 + b[0] * 0.06 + 0.7, a[1] * 0.94 + b[1] * 0.06 - 1.1]);
        }
        add(points, "contour", 1, 0.34, 0, "rolled-rim");
      }
    });
    // Three pointed sepals under the bowl, then two unequal edges of the thorny stem.
    [[244,452, 240,478, 253,491, 272,499], [272,499, 263,468, 282,470, 286,457],
      [311,457, 319,493, 352,510, 366,506], [366,506, 337,489, 336,461, 330,449],
      [299,460, 286,503, 297,519, 306,528], [306,528, 301,498, 313,481, 314,465],
      [305,485, 324,591, 272,701, 297,820], [315,487, 331,589, 287,721, 303,821],
      [306,607, 302,594, 288,594, 283,590], [283,590, 286,609, 295,615, 304,618],
      [299,704, 309,693, 317,692, 319,685], [319,685, 321,706, 307,715, 297,717],
      [310,586, 290,567, 276,568, 260,563], [309,641, 328,621, 341,622, 351,618],
      [294,715, 273,709, 264,703, 252,697]].forEach(function (c) { pen(c, "contour", 0, 0.52, "stem-sepal"); });

    // Leaves have distinct axes, unequal lobes and small backwards-facing teeth.
    var leaves = [
      [[278,579, 229,568, 148,516, 103,526], [278,579, 235,516, 161,497, 103,526], [278,579, 210,628, 146,550, 103,526]],
      [[330,635, 372,611, 433,588, 480,580], [330,635, 352,578, 422,565, 480,580], [330,635, 383,661, 446,605, 480,580]],
      [[272,702, 245,735, 210,783, 187,796], [272,702, 211,702, 187,755, 187,796], [272,702, 288,762, 226,795, 187,796]]
    ];
    leaves.forEach(function (leaf, id) {
      pen(leaf[0], "contour", 0, 0.67, "midrib-" + id);
      for (var side = 1; side <= 2; side++) {
        var points = [];
        for (var j = 0; j <= 52; j++) {
          var u = j / 52, a = cubic(leaf[side], u), mid = cubic(leaf[0], u);
          var tooth = j % 4 === 2 ? 0.13 : j % 4 === 3 ? -0.025 : 0;
          points.push([a[0] + (a[0] - mid[0]) * tooth, a[1] + (a[1] - mid[1]) * tooth]);
        }
        add(points, "contour", 0, side === 1 ? 0.59 : 0.4, 0, "serrated-leaf-" + id);
      }
    });

    // Bounded surface coordinates bend the hatching with the petal's cup and lip.
    // Uneven spacing, truncated ends and local clusters leave dark breathing room.
    function surface(p, u, v) {
      var a = cubic(p[0], u), b = cubic(p[1], u);
      return [a[0] + (b[0] - a[0]) * v, a[1] + (b[1] - a[1]) * v];
    }
    // Broad faces between the rolled rims. They are deliberately separate from the
    // edge ribbons: sparse, long marks turn into little cross-hatched pockets.
    var faces = [
      [[265,248, 277,232, 337,226, 351,264], [265,248, 278,218, 343,211, 351,264]],
      [[251,286, 272,320, 331,319, 348,278], [251,286, 251,353, 332,350, 348,278]],
      [[235,211, 260,169, 336,178, 374,229], [235,211, 269,242, 338,214, 374,229]],
      [[215,277, 200,291, 208,333, 224,350], [215,277, 199,235, 212,226, 231,241]],
      [[224,350, 264,374, 335,352, 365,329], [224,350, 267,352, 343,341, 365,329]],
      [[168,350, 198,388, 224,403, 256,387], [168,350, 180,420, 250,427, 256,387]],
      [[256,387, 318,410, 374,374, 417,390], [256,387, 320,428, 386,415, 417,390]],
      [[351,266, 370,286, 356,313, 341,329], [351,266, 387,245, 404,274, 341,329]]
    ];
    faces.forEach(function (p, id) {
      for (var j = 0; j < (low ? 43 : 95); j++) {
        var u = 0.03 + random()*0.94, start = random()*0.15, extent = 0.3 + random()*0.65;
        var points = [];
        for (var k = 0; k <= 12; k++) {
          var v = start + (extent-start)*k/12;
          points.push(surface(p, Math.max(0.01, Math.min(0.99, u+Math.sin(v*3.14)*0.055)), v));
        }
        add(points, "shading", 1, 0.20+random()*0.30, 0, "face-hatch-"+id);
        if (j%5 === 0) {
          points = [];
          for (k = 0; k <= 8; k++) points.push(surface(p, Math.min(0.99,u+k*0.005), 0.15+k*0.025));
          add(points, "shading", 1, 0.24, 0, "face-cross-hatch");
        }
      }
    });
    PETALS.forEach(function (p, id) {
      var count = (low ? 39 : 83) + id * (low ? 1 : 2);
      for (var j = 0; j < count; j++) {
        var u = 0.025 + random() * 0.95, start = random() * 0.14;
        var extent = j % 4 === 0 ? 0.8 : (0.18 + random() * 0.48) * p[2];
        var points = [], bend = (random() - 0.38) * 0.1;
        for (var k = 0; k <= 9; k++) {
          var v = start + extent * k / 9;
          points.push(surface(p, Math.max(0.01, Math.min(0.99, u + bend * Math.sin(v * 3.14))), v));
        }
        add(points, "shading", 1, 0.24 + random() * 0.32, 0, "curl-hatch-" + id);
        if (j % 4 === 0) {
          points = [];
          var crossV = 0.08 + random() * 0.26;
          for (k = 0; k <= 6; k++) points.push(surface(p, Math.min(0.99, u + k * 0.007), crossV + k * 0.017));
          add(points, "shading", 1, 0.22 + random() * 0.17, 0, "cross-hatch");
        }
      }
      // Short, confident pressure accents under the curl, interspersed with softer graphite.
      var accent = [];
      for (j = 0; j <= 15; j++) accent.push(surface(p, 0.34 + j * 0.027, 0.03));
      add(accent, "shading", 1, 0.7, 0, "fold-accent");
    });
    leaves.forEach(function (leaf) {
      for (var side = 1; side <= 2; side++) {
        for (var j = 1; j <= 9; j++) {
          var u = j / 11, a = cubic(leaf[0], u - 0.055), b = cubic(leaf[side], u + 0.065);
          pen([a[0],a[1], a[0]*0.7+b[0]*0.3,a[1]*0.8+b[1]*0.2,
            a[0]*0.3+b[0]*0.7,a[1]*0.3+b[1]*0.7, b[0],b[1]], "shading", 0, 0.42, "leaf-vein");
        }
        for (j = 0; j < (low ? 40 : 90); j++) {
          u = 0.02 + random() * 0.94;
          var p = [leaf[0], leaf[side]], points = [];
          var reach = 0.2 + random() * 0.65;
          for (var k = 0; k <= 6; k++) points.push(surface(p, Math.min(0.99, u + k * 0.008), 0.12 + reach * k / 6));
          add(points, "shading", 0, 0.15 + random() * 0.24, 0, "leaf-hatch");
        }
      }
    });
    for (var j = 0; j < (low ? 42 : 85); j++) {
      var u = random(), a = cubic([309,491, 327,591, 280,713, 300,817], u);
      pen([a[0]-2,a[1], a[0]-1,a[1]+2, a[0]+1,a[1]+6, a[0]+1.5,a[1]+8], "shading", 0, 0.25, "stem-grain");
    }
    // Dew is drawn as tiny incomplete ellipses, a lower arc and a separate glancing highlight.
    [[272,190,3,4], [349,183,4,5.5], [385,254,3.2,4.7], [241,323,4.1,5],
      [296,377,3,4.2], [180,394,4,5], [405,364,2.4,3.2]].forEach(function (d) {
      oval(d[0], d[1], d[2], d[3], -0.4, "details", 1, 0.38, "dew");
      pen([d[0]-2,d[1]+3, d[0],d[1]+6, d[0]+3,d[1]+4, d[0]+4,d[1]+2], "details", 1, 0.28, "dew-shadow");
      add([[d[0]-1.6,d[1]-1], [d[0]-1,d[1]-2.1], [d[0]+0.2,d[1]-2.5]], "details", 1, 1, 3, "dew-highlight");
    });

    var schedule = schedulePens(strokes, seed, low);
    return { seed: seed >>> 0, lowQuality: !!low, strokes: strokes, count: strokes.length,
      schedule: schedule, petals: PETALS.map(function (p) { return curve(p[0]).concat(curve(p[1]).reverse()); }),
      headBounds: [108/600, 139/900, 467/600, 466/900],
      totalLength: strokes.reduce(function (sum, s) { return sum + s.length; }, 0),
      ignition: IGNITION, aspect: 2 / 3 };
  }
  // Below the cup, distance follows the stem before branching toward a leaf.
  function burnTime(x, y) {
    var px = x*600, py = y*900, ix = IGNITION[0]*600, iy = IGNITION[1]*900;
    var path = py <= 460 ? Math.hypot(px-ix,py-iy) :
      Math.hypot(305-ix,460-iy)+(py-460)+Math.abs(px-305)*0.8;
    return TIMELINE.burn + path/390;
  }
  // Distances use paper-height units, including the actual graphite wobble.
  function distance(x, y, ex, ey) { return Math.hypot((ex-x)*2/3, ey-y); }
  function ease(u) { u = Math.max(0, Math.min(1, u)); return u - Math.sin(2*Math.PI*u)*0.45/(2*Math.PI); }
  function schedulePens(strokes, seed, low) {
    var count = low ? PEN_COUNT.reduced : PEN_COUNT.desktop, random = randomSource(seed ^ 0x73a91);
    var pens = [], stages = [], kinds = ["gesture", "contour"];
    // Reserve a quarter second of visible fan-out before the first graphite marks.
    var edges = [TIMELINE.gesture+0.25, TIMELINE.contour, TIMELINE.sweep];
    strokes.forEach(function (s) {
      var p = s.points, lengths = new Float64Array(p.length/2);
      for (var j = 1; j < lengths.length; j++) lengths[j] = lengths[j-1]+distance(p[j*2-2],p[j*2-1],p[j*2],p[j*2+1]);
      s.lengths = lengths; s.arcLength = lengths[lengths.length-1];
      s.topY = 1;
      for (var k = 1; k < p.length; k += 2) s.topY = Math.min(s.topY,p[k]);
    });
    // Split contiguous territories by their drawing load, not their number of marks.
    function territories(ids, slots) {
      if (slots === 1 || ids.length < 2) return [ids];
      var minX = 1, minY = 1, maxX = 0, maxY = 0, total = 0;
      ids.forEach(function (id) {
        var s = strokes[id], p = s.points;
        minX = Math.min(minX,p[0]); maxX = Math.max(maxX,p[0]);
        minY = Math.min(minY,p[1]); maxY = Math.max(maxY,p[1]); total += s.arcLength;
      });
      var axis = (maxX-minX)*2/3 > maxY-minY ? 0 : 1, leftSlots = Math.floor(slots/2);
      ids.sort(function (a,b) { return strokes[a].points[axis]-strokes[b].points[axis] || a-b; });
      var cut = 1, sum = strokes[ids[0]].arcLength, target = total*leftSlots/slots;
      while (cut < ids.length-1 && Math.abs(sum+strokes[ids[cut]].arcLength-target) < Math.abs(sum-target)) sum += strokes[ids[cut++]].arcLength;
      cut = Math.max(Math.min(leftSlots,ids.length-1),Math.min(cut,ids.length-(slots-leftSlots)));
      return territories(ids.slice(0,cut),leftSlots).concat(territories(ids.slice(cut),slots-leftSlots));
    }
    for (var i = 0; i < count; i++) {
      pens.push({ id: i, x: HEART[0], y: HEART[1], initialX: HEART[0], initialY: HEART[1],
        variation: 0.96+random()*0.08, ids: [] });
    }
    kinds.forEach(function (kind, stage) {
      var group = strokes.filter(function (s) { return s.kind === kind; }).map(function (s) { return s.id; });
      var available = pens.slice(), routes = [], maxWork = 0;
      function newRoute(pen, ids) {
        var r = { pen: pen.id, ids: [], pending: ids, work: 0, drawLength: 0, travelLength: 0 };
        routes.push(r); return r;
      }
      function append(r, id) {
        var pen = pens[r.pen], s = strokes[id], p = s.points;
        var travel = Math.max(0.00001,distance(pen.x,pen.y,p[0],p[1]));
        s.pen = pen.id; s.moveX = pen.x; s.moveY = pen.y;
        s.moveStart = r.work; r.work += travel/(TRAVEL_RATIO*pen.variation); s.start = r.work;
        r.work += s.arcLength/pen.variation; s.end = r.work;
        r.drawLength += s.arcLength; r.travelLength += travel;
        pen.x = p[p.length-2]; pen.y = p[p.length-1]; pen.ids.push(id); r.ids.push(id);
        maxWork = Math.max(maxWork,r.work);
      }
      function takePen(ids) {
        var x = 0, y = 0;
        ids.forEach(function (id) { x += strokes[id].points[0]; y += strokes[id].points[1]; });
        x /= ids.length; y /= ids.length;
        var nearest = 0, best = Infinity;
        available.forEach(function (pen,index) { var d = distance(pen.x,pen.y,x,y); if (d < best) { best = d; nearest = index; } });
        return available.splice(nearest,1)[0];
      }
      var workers = territories(group,Math.min(available.length,group.length)).map(function (ids) { return newRoute(takePen(ids),ids); });
      var remaining = group.length;
      while (remaining > 0) {
        // Deal the next stroke to the first free pen. It exhausts its own area
        // before helping the nearest unfinished area, avoiding idle workers.
        var worker = workers[0];
        workers.forEach(function (r) { if (r.work < worker.work) worker = r; });
        var pen = pens[worker.pen], owner = worker, nearest = 0, best = Infinity;
        var candidates = worker.pending.length ? [worker] : workers;
        candidates.forEach(function (r) {
          r.pending.forEach(function (id,j) {
            var p = strokes[id].points, d = distance(pen.x,pen.y,p[0],p[1]);
            if (d < best) { best = d; owner = r; nearest = j; }
          });
        });
        append(worker,owner.pending.splice(nearest,1)[0]); remaining--;
      }
      var speed = maxWork/(edges[stage+1]-edges[stage]), end = edges[stage];
      var speeds = new Float64Array(count), travelSpeeds = new Float64Array(count);
      routes.forEach(function (r) {
        speeds[r.pen] = speed*pens[r.pen].variation; travelSpeeds[r.pen] = speeds[r.pen]*TRAVEL_RATIO;
        r.ids.forEach(function (id) {
          var s = strokes[id], launch = edges[stage];
          s.moveStart = launch+s.moveStart/speed;
          s.start = launch+s.start/speed; s.end = launch+s.end/speed; end = Math.max(end,s.end);
        });
      });
      stages.push({ kind: kind, start: stage ? edges[stage] : TIMELINE.gesture, end: end, speed: speed, speeds: speeds, travelSpeeds: travelSpeeds });
    });
    pens.forEach(function (pen) {
      // Pens without a gesture join the same fan-out and take a longer visible trip.
      strokes[pen.ids[0]].moveStart = TIMELINE.gesture;
      pen.strokeIds = new Uint32Array(pen.ids);
      // [start time, end time, from x, from y, to x, to y] for each pen-up segment.
      pen.travel = new Float64Array(pen.ids.length*6);
      pen.ids.forEach(function (id,j) {
        var s = strokes[id], offset = j*6;
        pen.travel.set([s.moveStart,s.start,s.moveX,s.moveY,s.points[0],s.points[1]],offset);
      });
      delete pen.ids;
    });
    var top = Math.min.apply(null,strokes.map(function (s) { return s.topY; }));
    var bottom = 0;
    strokes.forEach(function (s) { for (var k = 1; k < s.points.length; k += 2) bottom = Math.max(bottom,s.points[k]); });
    // The band crosses the last mark early enough to finish its growing prefix.
    var sweepDuration = TIMELINE.hold-TIMELINE.sweep-0.3;
    strokes.forEach(function (s) {
      if (s.kind === "gesture" || s.kind === "contour") return;
      s.pen = -1;
      s.moveStart = TIMELINE.sweep;
      s.start = TIMELINE.sweep+(s.topY-top)/(bottom-top)*sweepDuration+random()*0.05;
      s.end = s.start+0.12+random()*0.13;
    });
    var completionOrder = new Uint32Array(strokes.map(function (s) { return s.id; }).sort(function (a,b) { return strokes[a].end-strokes[b].end || a-b; }));
    return { penCount: count, pens: pens, stages: stages, completionOrder: completionOrder, travelRatio: TRAVEL_RATIO, sweepTop: top, sweepBottom: bottom, sweepDuration: sweepDuration };
  }
  // Writes [x, y, segment index, segment fraction] into a caller-owned buffer.
  // Progress means fraction of arc length, never a fraction of authoring knots.
  function partialReveal(s, progress, out) {
    var u = Math.max(0,Math.min(1,progress)), distance = u*s.arcLength, lengths = s.lengths;
    var lo = 0, hi = lengths.length-1;
    while (lo+1 < hi) { var mid = (lo+hi) >>> 1; if (lengths[mid] <= distance) lo = mid; else hi = mid; }
    var f = (distance-lengths[lo])/(lengths[lo+1]-lengths[lo]), p = s.points;
    out[0] = p[lo*2]+(p[lo*2+2]-p[lo*2])*f; out[1] = p[lo*2+1]+(p[lo*2+3]-p[lo*2+1])*f;
    out[2] = lo; out[3] = f;
    return out;
  }
  // Stateless sampling also gives the tails their true recent path on direct seeks.
  // Output: [x, y, visibility, drawing stroke id (-1 for pen up), path progress].
  function samplePen(rose, penId, t, out) {
    var pen = rose.schedule.pens[penId], ids = pen.strokeIds, strokes = rose.strokes;
    var lo = 0, hi = ids.length;
    while (lo < hi) { var mid = (lo+hi) >>> 1; if (strokes[ids[mid]].end < t) lo = mid+1; else hi = mid; }
    out[3] = -1; out[4] = 0;
    if (lo === ids.length) { out[0] = pen.x; out[1] = pen.y; out[2] = 0.28; }
    else {
      var s = strokes[ids[lo]];
      if (t < s.start) {
        var u = Math.max(0,Math.min(1,(t-s.moveStart)/(s.start-s.moveStart)));
        u = u*u*(3-2*u);
        out[0] = s.moveX+(s.points[0]-s.moveX)*u; out[1] = s.moveY+(s.points[1]-s.moveY)*u; out[2] = 0.28;
      } else {
        var progress = ease((t-s.start)/(s.end-s.start));
        partialReveal(s,progress,out); out[2] = 1; out[3] = s.id; out[4] = progress;
      }
    }
    if (t >= 0 && t < TIMELINE.gesture) out[2] = 1;
    if (t < 0) out[2] = 0;
    if (t >= TIMELINE.sweep) {
      var exit = Math.max(0,Math.min(1,(t-TIMELINE.sweep)/0.6));
      var turn = penId/rose.schedule.penCount*Math.PI*2;
      out[0] = pen.x+Math.cos(turn)*exit*0.8;
      out[1] = pen.y+Math.sin(turn)*exit*0.5;
      out[2] = 0.55*(1-exit);
    }
    return out;
  }
  function wordBox(width, height) {
    var w = Math.min(320,width*0.34), h = w*0.66;
    return { x: (width-w)/2, y: height*0.36-h/2, width: w, height: h };
  }
  // All trajectories are analytic: direct seeks, resize and playback use identical paths.
  function sampleDrop(t, box, out) {
    var u = Math.max(0,Math.min(1,(t-TIMELINE.drop)/(TIMELINE.burn-TIMELINE.drop)));
    out[0] = box.x+IGNITION[0]*box.width;
    var landingY = box.y+IGNITION[1]*box.height;
    out[1] = -18+(landingY+18)*u*u;
    out[2] = t >= TIMELINE.drop && t < TIMELINE.burn ? 1 : 0;
    out[3] = 3+u*5;
    return out;
  }
  function buildParticles(rose, width, height, paper) {
    var random = randomSource(rose.seed ^ 0x5a7f10), box = wordBox(width,height);
    var targets = [], minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    SIGNATURE.forEach(function (c, stroke) {
      var points = curve(c,80);
      points.forEach(function (p) {
        minX = Math.min(minX,p[0]); maxX = Math.max(maxX,p[0]);
        minY = Math.min(minY,p[1]); maxY = Math.max(maxY,p[1]);
        targets.push({ x: p[0], y: p[1], letter: stroke === 15 ? -1 : stroke < 4 ? 0 : stroke < 7 ? 1 : stroke < 10 ? 2 : stroke < 13 ? 3 : 4 });
      });
    });
    var particles = [], wordCount = rose.lowQuality ? 900 : 1800, freeCount = rose.lowQuality ? 600 : 1400;
    for (var i = 0; i < wordCount+freeCount; i++) {
      var isWord = i < wordCount, target = targets[Math.floor(random()*targets.length)];
      var ux = (target.x-minX)/(maxX-minX), uy = (target.y-minY)/(maxY-minY);
      var arrival = TIMELINE.wordStart+ux*(TIMELINE.wordEnd-TIMELINE.wordStart);
      var stroke, k, sx, sy, release;
      // Early letters originate in the first burned petals; later letters also carry leaves.
      do {
        stroke = rose.strokes[Math.floor(random()*rose.strokes.length)];
        k = Math.floor(random()*stroke.points.length/2)*2;
        sx = stroke.points[k]; sy = stroke.points[k+1];
        release = Math.max(TIMELINE.ash,burnTime(sx,sy)+0.34);
      } while (isWord && release > arrival-0.35);
      var x = paper.x+sx*paper.width, y = paper.y+sy*paper.height;
      var tx = isWord ? box.x+ux*box.width : width+40+random()*width*0.3;
      var ty = isWord ? box.y+uy*box.height : y-80-random()*height*0.4;
      particles.push({ word: isWord, letter: target.letter, x: x, y: y, targetX: tx, targetY: ty,
        sourceX: sx, sourceY: sy, release: release,
        arrival: isWord ? arrival : 12.6+random()*0.9,
        cx: x+55+random()*width*0.23, cy: y-35-random()*height*0.16,
        // A curling wind lifts the ash before it approaches the writing from the left.
        bx: tx-(35+random()*90), by: ty-20-random()*75,
        phase: random()*Math.PI*2, sway: 5+random()*16 });
    }
    return { particles: particles, box: box, wordCount: wordCount };
  }
  // Output is CSS x/y, opacity and heat. No allocations in the animation loop.
  function sampleParticle(p, t, out) {
    var u = Math.max(0,Math.min(1,(t-p.release)/(p.arrival-p.release)));
    var v = 1-Math.pow(1-u,2), s = 1-v;
    var flutter = Math.sin(u*Math.PI)*Math.pow(1-u,1.5)*p.sway;
    out[0] = s*s*s*p.x+3*s*s*v*p.cx+3*s*v*v*p.bx+v*v*v*p.targetX+Math.sin(u*15+p.phase)*flutter;
    out[1] = s*s*s*p.y+3*s*s*v*p.cy+3*s*v*v*p.by+v*v*v*p.targetY+Math.cos(u*19+p.phase)*flutter;
    out[2] = t < p.release ? 0 : p.word ? 0.72 : 0.5*(1-u);
    out[3] = p.word ? Math.max(0,1-Math.max(0,t-p.arrival)/0.6) : 0;
    return out;
  }
  return { buildRose: buildRose, randomSource: randomSource, burnTime: burnTime, TIMELINE: TIMELINE,
    wordBox: wordBox, sampleDrop: sampleDrop, buildParticles: buildParticles, sampleParticle: sampleParticle,
    PEN_COUNT: PEN_COUNT, partialReveal: partialReveal, samplePen: samplePen };
});
