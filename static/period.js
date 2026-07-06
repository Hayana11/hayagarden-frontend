// Period / cycle tracker overlay for calendar.html
(function () {
  var modalDate = null;

  function pad(n) { return n < 10 ? '0' + n : String(n); }
  function todayStr() {
    var t = new Date();
    return t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate());
  }

  async function getRecords(year, month) {
    var r = await fetch('/api/period/records?year=' + year + '&month=' + month);
    var d = await r.json();
    return d.records || [];
  }

  async function getStats() {
    var r = await fetch('/api/period/stats');
    return await r.json();
  }

  async function refreshPeriod() {
    try {
      var results = await Promise.all([getRecords(calYear, calMonth + 1), getStats()]);
      renderPeriodStats(results[1]);
      overlayPeriodOnCal(results[0], results[1]);
    } catch (e) { console.warn('period refresh error', e); }
  }

  function overlayPeriodOnCal(records, stats) {
    var periodDates = new Set();
    var sexDates = new Set();
    records.forEach(function (r) {
      if (r.type === 'period') periodDates.add(r.date);
      else if (r.type === 'sex') sexDates.add(r.date);
    });
    // 后端聚合的实际经期日（含费佳 start/end 记录展开的段），比每日打点更全
    (stats.period_days || []).forEach(function (d) { periodDates.add(d); });

    var predDates = new Set();
    var ovulDate = null;
    var fertileDates = new Set();

    if (stats.predicted_days && stats.predicted_days.length) {
      stats.predicted_days.forEach(function (d) { predDates.add(d); });
    } else if (stats.next_period) {
      var np = new Date(stats.next_period + 'T00:00:00');
      var n = stats.period_length || 5;
      for (var i = 0; i < n; i++) {
        var pd = new Date(np);
        pd.setDate(np.getDate() + i);
        predDates.add(pd.getFullYear() + '-' + pad(pd.getMonth() + 1) + '-' + pad(pd.getDate()));
      }
    }
    if (stats.ovulation) {
      ovulDate = stats.ovulation;
      var ov = new Date(stats.ovulation + 'T00:00:00');
      for (var j = -2; j <= 2; j++) {
        var fd = new Date(ov);
        fd.setDate(ov.getDate() + j);
        fertileDates.add(fd.getFullYear() + '-' + pad(fd.getMonth() + 1) + '-' + pad(fd.getDate()));
      }
    }

    var cells = document.querySelectorAll('#cal-grid .cal-day[data-date]');
    cells.forEach(function (cell) {
      var date = cell.getAttribute('data-date');
      cell.classList.remove('pd', 'pd-pred', 'ovul', 'fertile');
      var old = cell.querySelector('.pd-heart');
      if (old) old.remove();

      if (periodDates.has(date)) {
        cell.classList.add('pd');
      } else if (date === ovulDate) {
        cell.classList.add('ovul');
      } else if (fertileDates.has(date)) {
        cell.classList.add('fertile');
      } else if (predDates.has(date)) {
        cell.classList.add('pd-pred');
      }

      if (sexDates.has(date)) {
        cell.style.position = 'relative';
        var dot = document.createElement('span');
        dot.className = 'pd-heart';
        dot.textContent = '♥';
        cell.appendChild(dot);
      }
    });
  }

  function fmtMD(ds) {
    if (!ds) return '';
    var p = ds.split('-');
    return (+p[1]) + '月' + (+p[2]) + '日';
  }

  function renderPeriodStats(stats) {
    var el = document.getElementById('period-stats');
    if (!el) return;
    if (!stats.last_period) {
      el.innerHTML = '<div class="empty-hint">暂无数据，先记录第一次经期吧</div>';
      return;
    }

    // ── hero：下次预测大字 + 周期进度条 ──
    var hero = '';
    if (stats.next_period) {
      var du = stats.days_until;
      var main, sub, late = du != null && du < 0;
      if (late) {
        main = '已推迟 <span class="ch-accent">' + Math.abs(du) + '</span> 天';
        sub = '原预计 ' + fmtMD(stats.next_period) + ' · 通常持续 ' + (stats.period_length || 5) + ' 天';
      } else if (du === 0) {
        main = '预计<span class="ch-accent">今天</span>来';
        sub = '通常持续 ' + (stats.period_length || 5) + ' 天';
      } else {
        main = '下次 <span class="ch-accent">' + fmtMD(stats.next_period) + '</span>';
        sub = '还有 ' + du + ' 天 · 预计持续 ' + (stats.period_length || 5) + ' 天';
      }
      var bar = '';
      if (stats.cycle_day && stats.cycle_length) {
        var pct = Math.min(stats.cycle_day / stats.cycle_length * 100, 100);
        bar = '<div class="ch-bar"><div class="ch-fill' + (late ? ' late' : '') + '" style="width:' + pct + '%"></div></div>'
          + '<div class="ch-bar-labels"><span>周期第 ' + stats.cycle_day + ' 天</span><span>' + stats.cycle_length + ' 天/周期</span></div>';
      }
      hero = '<div class="cycle-hero"><div class="ch-label">CYCLE</div>'
        + '<div class="ch-main' + (late ? ' late' : '') + '">' + main + '</div>'
        + '<div class="ch-sub">' + sub + '</div>' + bar + '</div>';
    }

    // ── 小字统计行 ──
    var mini = [];
    if (stats.last_period) mini.push('上次 <b>' + fmtMD(stats.last_period) + '</b>');
    if (stats.cycle_length) mini.push('平均周期 <b>' + stats.cycle_length + '天</b>');
    if (stats.ovulation) mini.push('排卵日 <b>' + fmtMD(stats.ovulation) + '</b>');
    var miniHtml = mini.length ? '<div class="ps-mini">' + mini.map(function(m){return '<span>'+m+'</span>';}).join('') + '</div>' : '';

    el.innerHTML = hero + miniHtml;
  }

  async function openDayModal(dateStr) {
    modalDate = dateStr;
    document.getElementById('dm-title').textContent = dateStr;
    document.getElementById('dm-list').innerHTML = '<div class="empty-hint">loading…</div>';
    document.getElementById('day-modal').classList.add('show');
    var r = await fetch('/api/period/records?date=' + dateStr);
    var d = await r.json();
    renderDmList(d.records || []);
  }

  function renderDmList(recs) {
    var el = document.getElementById('dm-list');
    if (!recs.length) {
      el.innerHTML = '<div class="empty-hint">当天暂无记录</div>';
      return;
    }
    var LABELS = { period: '🩸 经期', sex: '♥ 爱爱', start: '🩸 经期开始', end: '🩸 经期结束' };
    el.innerHTML = recs.map(function (r) {
      var label = LABELS[r.type] || r.type;
      if (r.note) label += ' · ' + r.note;
      return '<div class="dm-rec"><span>' + label + '</span>' +
             '<button class="dm-del" onclick="dmDelete(' + r.id + ')">删除</button></div>';
    }).join('');
  }

  async function dmAdd(type) {
    if (!modalDate) return;
    await fetch('/api/period/records', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: modalDate, type: type })
    });
    openDayModal(modalDate);
    refreshPeriod();
  }

  async function dmDelete(id) {
    await fetch('/api/period/records/' + id, { method: 'DELETE' });
    openDayModal(modalDate);
    refreshPeriod();
  }

  function closeDayModal() {
    document.getElementById('day-modal').classList.remove('show');
    modalDate = null;
  }

  async function quickAddPeriod() {
    await fetch('/api/period/records', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: todayStr(), type: 'period' })
    });
    refreshPeriod();
  }

  async function quickAddSex() {
    await fetch('/api/period/records', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: todayStr(), type: 'sex' })
    });
    refreshPeriod();
  }

  window.refreshPeriod = refreshPeriod;
  window.openDayModal = openDayModal;
  window.closeDayModal = closeDayModal;
  window.dmAdd = dmAdd;
  window.dmDelete = dmDelete;
  window.quickAddPeriod = quickAddPeriod;
  window.quickAddSex = quickAddSex;

  // Kick off initial load
  refreshPeriod();
})();
