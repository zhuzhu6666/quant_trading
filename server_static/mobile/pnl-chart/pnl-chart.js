(function () {
  const INITIAL_CAPITAL = 500;
  const TOKEN_KEY = 'quant_h5_token';
  const chartEl = document.getElementById('chart');
  const emptyEl = document.getElementById('empty');
  const legendEl = document.getElementById('legend');
  const refreshBtn = document.getElementById('refreshBtn');
  const rangeTabs = Array.from(document.querySelectorAll('.range-tab'));
  const recentTradesEl = document.getElementById('recentTrades');

  let chart = null;
  let pnlSeries = null;
  let chartData = [];
  let tradeRows = [];
  let activeRange = 120;

  function getToken() {
    const params = new URLSearchParams(window.location.search);
    const hashParams = new URLSearchParams((window.location.hash || '').replace(/^#/, ''));
    const token = params.get('token') || hashParams.get('token') || sessionStorage.getItem(TOKEN_KEY) || '';
    if (params.get('token') || hashParams.get('token')) {
      sessionStorage.setItem(TOKEN_KEY, token);
      params.delete('token');
      hashParams.delete('token');
      const cleanUrl = `${window.location.pathname}${params.toString() ? `?${params}` : ''}${hashParams.toString() ? `#${hashParams}` : ''}`;
      window.history.replaceState(null, '', cleanUrl);
    }
    return token;
  }

  function formatMoney(value, signed) {
    const n = Number(value || 0);
    const sign = signed && n > 0 ? '+' : '';
    return `${sign}${n.toFixed(2)}`;
  }

  function formatPct(value) {
    return `${(Number(value || 0) * 100).toFixed(2)}%`;
  }

  function toneClass(value) {
    const n = Number(value || 0);
    if (n > 0) return 'positive';
    if (n < 0) return 'negative';
    return '';
  }

  function setText(id, value, toneValue) {
    const node = document.getElementById(id);
    if (!node) return;
    node.textContent = value;
    node.classList.remove('positive', 'negative');
    const tone = toneClass(toneValue);
    if (tone) node.classList.add(tone);
  }

  function uniqueTimeRows(points) {
    const rows = [];
    let lastTime = 0;
    points.forEach((point) => {
      const rawTime = Math.floor(Number(point.ts || 0));
      if (!rawTime || !Number.isFinite(rawTime)) return;
      const time = rawTime <= lastTime ? lastTime + 1 : rawTime;
      lastTime = time;
      const cumulative = Number(point.cumulative || 0);
      const pnl = Number(point.pnl || 0);
      rows.push({
        time,
        pnl,
        cumulative,
        equity: INITIAL_CAPITAL + cumulative,
      });
    });
    return rows;
  }

  function formatTime(time) {
    const date = new Date(Number(time || 0) * 1000);
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hour = String(date.getHours()).padStart(2, '0');
    const minute = String(date.getMinutes()).padStart(2, '0');
    return `${month}-${day} ${hour}:${minute}`;
  }

  function createPnlChart() {
    if (!window.LightweightCharts) {
      throw new Error('lightweight-charts not loaded');
    }
    if (chart) return;
    chart = LightweightCharts.createChart(chartEl, {
      autoSize: true,
      layout: {
        background: { type: LightweightCharts.ColorType.Solid, color: '#ffffff' },
        textColor: '#5f6b7a',
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      },
      grid: {
        vertLines: { color: '#edf2f7' },
        horzLines: { color: '#edf2f7' },
      },
      localization: {
        priceFormatter: (price) => formatMoney(price, true),
      },
      rightPriceScale: {
        borderColor: '#e5edf5',
        scaleMargins: { top: 0.14, bottom: 0.18 },
      },
      timeScale: {
        borderColor: '#e5edf5',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 8,
        barSpacing: 8,
        fixLeftEdge: false,
        fixRightEdge: false,
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
    });

    if (chart.addSeries && LightweightCharts.BaselineSeries) {
      pnlSeries = chart.addSeries(LightweightCharts.BaselineSeries, {
        baseValue: { type: 'price', price: 0 },
        topLineColor: '#16a34a',
        topFillColor1: 'rgba(22, 163, 74, 0.22)',
        topFillColor2: 'rgba(22, 163, 74, 0.02)',
        bottomLineColor: '#dc2626',
        bottomFillColor1: 'rgba(220, 38, 38, 0.02)',
        bottomFillColor2: 'rgba(220, 38, 38, 0.22)',
        lineWidth: 2,
        priceLineVisible: true,
        lastValueVisible: true,
      });
    } else {
      pnlSeries = chart.addBaselineSeries({
        baseValue: { type: 'price', price: 0 },
        topLineColor: '#16a34a',
        bottomLineColor: '#dc2626',
        lineWidth: 2,
      });
    }

    chart.subscribeCrosshairMove((param) => {
      if (!param || !param.time || !param.seriesData) {
        renderLegend();
        return;
      }
      const value = param.seriesData.get(pnlSeries);
      const row = tradeRows.find((item) => item.time === param.time);
      const pnlValue = typeof value === 'number' ? value : value && value.value;
      if (!row && pnlValue === undefined) {
        renderLegend();
        return;
      }
      const cumulative = row ? row.cumulative : pnlValue;
      const equity = INITIAL_CAPITAL + Number(cumulative || 0);
      legendEl.innerHTML = [
        `<span>${formatTime(param.time)}</span>`,
        `<span class="${toneClass(row && row.pnl)}">单笔 ${formatMoney(row ? row.pnl : 0, true)}</span>`,
        `<span class="${toneClass(cumulative)}">累计 ${formatMoney(cumulative, true)}</span>`,
        `<span>权益 ${formatMoney(equity, false)}</span>`,
      ].join(' · ');
    });
  }

  function renderLegend() {
    const last = tradeRows[tradeRows.length - 1];
    if (!last) {
      legendEl.textContent = '拖动平移，双指缩放，点按查看十字线';
      return;
    }
    legendEl.innerHTML = [
      `<span>${formatTime(last.time)}</span>`,
      `<span class="${toneClass(last.pnl)}">单笔 ${formatMoney(last.pnl, true)}</span>`,
      `<span class="${toneClass(last.cumulative)}">累计 ${formatMoney(last.cumulative, true)}</span>`,
      `<span>权益 ${formatMoney(last.equity, false)}</span>`,
    ].join(' · ');
  }

  function applyRange(range) {
    activeRange = range;
    rangeTabs.forEach((button) => {
      const value = button.dataset.range === 'all' ? 'all' : Number(button.dataset.range);
      button.classList.toggle('active', value === range);
    });
    if (!chartData.length) return;
    const scale = chart.timeScale();
    if (range === 'all' || chartData.length <= range) {
      scale.fitContent();
      return;
    }
    scale.setVisibleLogicalRange({
      from: Math.max(chartData.length - range, 0),
      to: chartData.length + 6,
    });
  }

  function renderRecentTrades() {
    const rows = tradeRows.slice(-8).reverse();
    recentTradesEl.innerHTML = rows.map((row) => (
      `<div class="trade-row">
        <div>${formatTime(row.time)}</div>
        <div class="trade-metrics">
          <span class="${toneClass(row.pnl)}">${formatMoney(row.pnl, true)}</span>
          <span class="${toneClass(row.cumulative)}"> / ${formatMoney(row.cumulative, true)}</span>
          <span> / ${formatMoney(row.equity, false)}</span>
        </div>
      </div>`
    )).join('');
  }

  async function loadSeries() {
    const token = getToken();
    if (!token) {
      throw new Error('missing token');
    }
    const response = await fetch('/api/live/realized-pnl-series?scope=all', {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });
    if (!response.ok) {
      throw new Error(`API ${response.status}`);
    }
    return response.json();
  }

  async function refresh() {
    refreshBtn.disabled = true;
    refreshBtn.textContent = '刷新中';
    try {
      createPnlChart();
      const payload = await loadSeries();
      const summary = payload.summary || {};
      tradeRows = uniqueTimeRows(payload.points || []);
      chartData = tradeRows.map((row) => ({ time: row.time, value: row.cumulative }));
      const latest = tradeRows[tradeRows.length - 1] || null;
      const realized = Number(summary.realized_pnl ?? (latest ? latest.cumulative : 0));
      const equity = INITIAL_CAPITAL + realized;
      setText('equityValue', formatMoney(equity, false), realized);
      setText('pnlValue', formatMoney(realized, true), realized);
      setText('tradesValue', String(summary.trades || tradeRows.length), 0);
      setText('winRateValue', formatPct(summary.win_rate || 0), Number(summary.win_rate || 0) - 0.5);
      document.getElementById('chartSub').textContent = `初始资金 ${formatMoney(INITIAL_CAPITAL, false)} · 当前权益 ${formatMoney(equity, false)}`;

      pnlSeries.setData(chartData);
      emptyEl.hidden = chartData.length > 0;
      renderLegend();
      renderRecentTrades();
      applyRange(activeRange);
    } catch (err) {
      emptyEl.hidden = false;
      emptyEl.textContent = err && err.message === 'missing token'
        ? '请从小程序进入图表页'
        : `图表加载失败：${err && err.message ? err.message : err}`;
    } finally {
      refreshBtn.disabled = false;
      refreshBtn.textContent = '刷新';
    }
  }

  rangeTabs.forEach((button) => {
    button.addEventListener('click', () => {
      applyRange(button.dataset.range === 'all' ? 'all' : Number(button.dataset.range));
    });
  });
  refreshBtn.addEventListener('click', refresh);
  window.addEventListener('resize', () => {
    if (chart) chart.timeScale().fitContent();
    applyRange(activeRange);
  });

  refresh();
})();
