(() => {
  const board = document.querySelector('.mf-board');
  if (!board) return;
  const stages = [...board.querySelectorAll('.mf-stage')];
  const paths = board.querySelector('.mf-paths');
  const play = document.querySelector('.mf-play');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let animation = 0;

  function surface(canvas) {
    const {width, height} = canvas.getBoundingClientRect();
    if (!width || !height) return null;
    const scale = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.round(width * scale), h = Math.round(height * scale);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    ctx.setTransform(w / width, 0, 0, h / height, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return {ctx, width, height};
  }

  function line(ctx, points, opacity = 1) {
    ctx.globalAlpha = opacity;
    ctx.beginPath();
    for (const [i, [x,y]] of points.entries()) {
      if (i === 0) ctx.moveTo(x,y);
      else ctx.lineTo(x,y);
    }
    ctx.stroke();
  }
  function dot(ctx, x, y, radius, opacity) {
    ctx.globalAlpha = opacity;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 2 * Math.PI);
    ctx.fill();
  }

  function illustration(stage) {
    const canvas = stage.querySelector('.mf-illustration');
    const drawing = surface(canvas);
    if (!drawing) return;
    const {ctx, width, height} = drawing;
    const kind = stage.dataset.stage;
    const compact = kind === 'l4' || kind === 'op';
    const artWidth = compact ? 68 : 168, artHeight = compact ? 46 : 80;
    const scale = Math.min(width / artWidth, height / artHeight);
    ctx.translate((width - artWidth * scale) / 2, (height - artHeight * scale) / 2);
    ctx.scale(scale, scale);
    ctx.fillStyle = ctx.strokeStyle = getComputedStyle(canvas).color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = ctx.lineCap = 'round';

    if (kind === 'l1' || kind === 'l2') {
      // Events follow separate traces. The daily summary groups them into three bands.
      const bands = kind === 'l1' ? 7 : 3;
      for (let row = 0; row < bands; row++) {
        const trace = [];
        for (let col = 0; col < 25; col++) {
          const x = 8 + col * 6.3;
          const y = 40 + (row - (bands - 1)/2) * (bands === 7 ? 7 : 15)
            + Math.sin(col * .27 + row * .46) * (bands === 7 ? 6 : 3);
          trace.push([x,y]);
          if (bands === 7) dot(ctx, x, y, 1 + (col + row) % 3 * .26, .35 + (col % 5) * .13);
          else if (col % 6 === 0) dot(ctx, x, y, 2.5, .9);
        }
        if (bands === 3) line(ctx, trace, .75);
      }
    } else if (kind === 'l3') {
      const cx = 84, cy = 41;
      for (const r of [33, 24, 15]) {
        const hex = Array.from({length: 7}, (_, i) => {
          const angle = i * Math.PI / 3 - Math.PI / 2;
          return [cx + Math.cos(angle)*r, cy + Math.sin(angle)*r];
        });
        line(ctx, hex, r === 24 ? .95 : .46);
        if (r === 24) hex.slice(0,6).forEach(([x,y]) => dot(ctx,x,y,2.1,1));
      }
      line(ctx, [[63,29],[84,41],[105,29]], .9);
      line(ctx, [[84,41],[84,65]], .9);
      for (const sign of [-1,1]) {
        line(ctx, [[cx+sign*30,cy],[cx+sign*63,cy]], .36);
        dot(ctx, cx+sign*63, cy, 2.3, .95);
      }
    } else if (kind === 'l4') {
      for (const offset of [12,6,0]) {
        line(ctx, [[8,15+offset],[34,5+offset],[60,15+offset],[34,25+offset],[8,15+offset]], 1-offset*.043);
      }
    } else {
      line(ctx, [[2,26],[16,26],[22,20],[28,32],[35,7],[41,38],[49,23],[65,23]], 1);
      dot(ctx,65,23,2.3,1);
    }
  }

  function drawRoutes(progress = -1) {
    const drawing = surface(paths);
    if (!drawing) return;
    const {ctx} = drawing;
    const origin = paths.getBoundingClientRect();
    const boxes = stages.map(el => {
      const r = el.getBoundingClientRect();
      return {x:r.left-origin.left, y:r.top-origin.top, w:r.width, h:r.height};
    });
    const mobile = boxes[1].y > boxes[0].y + boxes[0].h / 2;
    for (const [source, dest] of [[0,1],[1,2],[2,3],[2,4]]) {
      const a = boxes[source], b = boxes[dest];
      let points;
      if (!mobile) {
        const x1 = a.x+a.w, x2 = b.x, mid = (x1+x2)/2;
        const y1 = a.y+a.h/2, y2 = b.y+b.h/2;
        points = [[x1,y1],[mid,y1],[mid,y2],[x2-2,y2]];
      } else if (source < 2) {
        const x = a.x+a.w/2;
        points = [[x,a.y+a.h],[x,b.y-2]];
      } else {
        const x = a.x+9, y = b.y+b.h/2;
        points = [[x,a.y+a.h],[x,y],[b.x-2,y]];
      }
      ctx.strokeStyle = ctx.fillStyle = getComputedStyle(stages[source]).getPropertyValue('--tone').trim();
      ctx.lineWidth = 1.5;
      ctx.lineJoin = ctx.lineCap = 'round';
      line(ctx, points, .65);
      const [endX,endY] = points.at(-1);
      const down = mobile && source < 2;
      line(ctx, down ? [[endX-3,endY-4],[endX,endY],[endX+3,endY-4]]
                     : [[endX-4,endY-3],[endX,endY],[endX-4,endY+3]], .95);
      if (progress < source || progress >= source + 1) continue;
      const segments = points.slice(1).map(([x,y],i) => Math.hypot(x-points[i][0],y-points[i][1]));
      let travel = (progress-source) * segments.reduce((sum,n) => sum+n, 0);
      for (const [i,length] of segments.entries()) {
        if (length && travel <= length) {
          const t = travel/length, [x,y] = points[i], [u,v] = points[i+1];
          dot(ctx,x+(u-x)*t,y+(v-y)*t,3,1);
          break;
        }
        travel -= length;
      }
    }
  }

  function redraw() {
    stages.forEach(illustration);
    drawRoutes();
  }
  play.addEventListener('click', () => {
    play.disabled = true;
    const start = performance.now();
    function frame(now) {
      const staticMode = reduced.matches;
      const progress = (now - start) / 670;
      stages.forEach((el,i) => el.classList.toggle('mf-active', staticMode || Math.min(i,3) === Math.floor(progress)));
      if (!staticMode) drawRoutes(progress);
      if (now - start < (staticMode ? 1050 : 2600)) {
        animation = requestAnimationFrame(frame);
      } else {
        stages.forEach(el => el.classList.remove('mf-active'));
        drawRoutes();
        play.disabled = false;
        animation = 0;
      }
    }
    animation = requestAnimationFrame(frame);
  });
  window.addEventListener('pagehide', () => {
    cancelAnimationFrame(animation);
    stages.forEach(el => el.classList.remove('mf-active'));
    play.disabled = false;
  });
  new ResizeObserver(redraw).observe(board);
  new MutationObserver(redraw).observe(document.documentElement, {attributes:true, attributeFilter:['data-theme']});
  play.hidden = false;
  redraw();
})();
