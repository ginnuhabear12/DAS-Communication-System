const pillConn = document.getElementById("pillConn");
const pillAlarm = document.getElementById("pillAlarm");

const vRsrp = document.getElementById("vRsrp");
const vRsrq = document.getElementById("vRsrq");
const vSinr = document.getElementById("vSinr");
const vRssi = document.getElementById("vRssi");

const btnPause = document.getElementById("btnPause");
const btnClear = document.getElementById("btnClear");

let paused = false;

function tsToLabel(ts) {
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

const ctx = document.getElementById("kpiChart");
const chart = new Chart(ctx, {
  type: "line",
  data: {
    labels: [],
    datasets: [
      { label: "RSRP (dBm)", data: [], tension: 0.2 },
      { label: "RSRQ (dB)", data: [], tension: 0.2 },
      { label: "SINR (dB)", data: [], tension: 0.2 },
      { label: "RSSI (dBm)", data: [], tension: 0.2 },
    ],
  },
  options: {
    responsive: true,
    animation: false,
    scales: {
      y: { beginAtZero: false },
    },
    plugins: {
      legend: { display: true },
    },
  },
});

function setAlarmPill(kpi) {
  // Mock alarm rule (tweak later):
  // critical if RSRP < -110 or SINR < 0
  const critical = (kpi.rsrp < -110) || (kpi.sinr < 0);
  pillAlarm.textContent = critical ? "Alarm: CRITICAL" : "Alarm: none";
  pillAlarm.classList.toggle("pillRed", critical);
}

async function poll() {
  if (paused) return;

  try {
    const res = await fetch("/api/kpi", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const kpi = await res.json();

    pillConn.textContent = "API: OK";
    pillConn.classList.remove("pillRed");

    vRsrp.textContent = kpi.rsrp;
    vRsrq.textContent = kpi.rsrq;
    vSinr.textContent = kpi.sinr;
    vRssi.textContent = kpi.rssi;

    setAlarmPill(kpi);

    const label = tsToLabel(kpi.ts);
    chart.data.labels.push(label);
    chart.data.datasets[0].data.push(kpi.rsrp);
    chart.data.datasets[1].data.push(kpi.rsrq);
    chart.data.datasets[2].data.push(kpi.sinr);
    chart.data.datasets[3].data.push(kpi.rssi);

    // keep last N points
    const N = 60;
    if (chart.data.labels.length > N) {
      chart.data.labels.shift();
      chart.data.datasets.forEach(ds => ds.data.shift());
    }

    chart.update();
  } catch (e) {
    pillConn.textContent = "API: DOWN";
    pillConn.classList.add("pillRed");
  }
}

btnPause.addEventListener("click", () => {
  paused = !paused;
  btnPause.textContent = paused ? "Resume" : "Pause";
});

btnClear.addEventListener("click", () => {
  chart.data.labels = [];
  chart.data.datasets.forEach(ds => (ds.data = []));
  chart.update();
});

setInterval(poll, 1000);
poll();