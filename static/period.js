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
      if (r.type === 'period' || r.type === 'start') periodDates.add(r.date);
      else if (r.type === 'sex') sexDates.add(r.date);
      // end / unknown: not intimacy, not a new cycle mark on the grid
    });

    var predDates = new Set();
    var ovulDate = null;
    var fertileDates = new Set();

    if (stats.next_period) {
      var np = new Date(stats.next_period + 'T00:00:00');
      for (var i = 0; i < 5; i++) {
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

  function renderPeriodStats(stats) {
    var el = document.getElementById('period-stats');
    if (!el) return;
    var rows = [];
    if (stats.last_period) rows.push({ label: '上次经期', val: stats.last_period });
    if (stats.cycle_length) rows.push({ label: '平均周期', val: stats.cycle_length + ' 天' });
    if (stats.next_period) rows.push({ label: '下次预测', val: stats.next_period });
    if (stats.ovulation) rows.push({ label: '排卵日预测', val: stats.ovulation });
    if (!rows.length) {
      el.innerHTML = '<div class="empty-hint">暂无数据，先记录第一次经期吧</div>';
      return;
    }
    el.innerHTML = rows.map(function (r) {
      return '<div class="ps-row"><span class="ps-label">' + r.label +
             '</span><span class="ps-val">' + r.val + '</span></div>';
    }).join('');
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

  function recordTypeLabel(type) {
    if (type === 'period' || type === 'start') return '🩸 经期';
    if (type === 'end') return '经期结束';
    if (type === 'sex') return '♥ 亲密';
    return '记录';
  }

  function renderDmList(recs) {
    var el = document.getElementById('dm-list');
    if (!recs.length) {
      el.innerHTML = '<div class="empty-hint">当天暂无记录</div>';
      return;
    }
    el.innerHTML = recs.map(function (r) {
      var label = recordTypeLabel(r.type);
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
