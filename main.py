import network
import socket
import uselect
import ujson
import utime

try:
    from m5stack import lcd, btnA, btnB, btnC, speaker, rgb, power
except ImportError:
    lcd = None
    speaker = None
    rgb = None
    power = None
    btnA = None
    btnB = None
    btnC = None


AP_SSID = "LabTimer"
AP_PASSWORD = "labtimer123"

HTTP_PORT = 80
DISPLAY_REFRESH_MS = 1000
RGB_REFRESH_MS = 200
PRESET_MENU_TIMEOUT_MS = 20000
MAX_TIMER_SEC = 24 * 60 * 60
DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 240

COLOR_BG = 0x000000
COLOR_HEADER = 0x14345B
COLOR_PANEL = 0x0B1724
COLOR_ACCENT = 0x42A5F5
COLOR_TEXT = 0xFFFFFF
COLOR_MUTED = 0x8CA0B3
COLOR_RUNNING = 0x27AE60
COLOR_WARNING = 0xF39C12
COLOR_EXPIRED = 0xE74C3C

RGB_OFF = 0x000000
RGB_IDLE = 0x001018
RGB_RUNNING = 0x004020
RGB_WARNING = 0x503000
RGB_EXPIRED = 0x600000
RGB_FLASH = 0x404040
LED_COUNT = 10
# Two side strips (5 + 5). Physical index 1 starts at right-top and wiring continues clockwise:
LED_CLOCKWISE_MAP = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
# Symmetric top-to-bottom row pairs: (right_side, left_side).
LED_SYMMETRIC_ROWS = [(1, 10), (2, 9), (3, 8), (4, 7), (5, 6)]

PRESETS = [
    {"id": 1, "title": "cDNA synthesis", "duration_sec": 26 * 60},
    {"id": 2, "title": "qPCR run", "duration_sec": 97 * 60},
    {"id": 3, "title": "Media Warmup", "duration_sec": 30 * 60},
    {"id": 4, "title": "Overnight Reaction", "duration_sec": 8 * 60 * 60},
]

MENU_ITEMS = [{"type": "preset", "title": p["title"], "data": p} for p in PRESETS]
MENU_ITEMS.append({"type": "action", "action": "show_ip", "title": "Show IP Address"})
MENU_ITEMS.append({"type": "action", "action": "toggle_fs", "title": "Toggle Fullscreen"})

timers = []
next_timer_id = 1
main_menu_open = False
main_menu_index = 0
preset_last_activity_ms = 0
fullscreen_mode = False
last_display_ms = 0
button_last_levels = {"A": False, "B": False, "C": False}
status_message = ""
last_rgb_ms = 0
rgb_blink_phase = False
user_must_clear_expired_alert = False
last_rgb_frame = None
display_static_ready = False
display_cache = {}
last_progress_fill = -1
last_progress_color = None
last_render_phase = None


INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>M5Stack FIRE Lab Timer</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; max-width: 900px; }
    h1, h2 { margin-bottom: 8px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; }
    .row > div { min-width: 180px; flex: 1; }
    label { font-weight: 600; display: block; margin-bottom: 4px; }
    input { width: 100%; padding: 8px; box-sizing: border-box; }
    button { padding: 8px 12px; cursor: pointer; }
    .meta { color: #5f6368; font-size: 0.9rem; }
    .preset { border: 1px solid #ddd; border-radius: 6px; padding: 10px; margin-bottom: 8px; }
    #status { min-height: 20px; font-weight: 600; }
    .error { color: #b00020; }
  </style>
</head>
<body>
  <h1>M5Stack FIRE Laboratory Timer</h1>
  <p class="meta">You can run multiple timers at once. The active timer is the one with the least time remaining.</p>

  <div class="card">
    <h2>Current Running Timer</h2>
    <div id="currentTimer">No active timer.</div>
  </div>

  <div class="card">
    <h2>Add Custom Timer</h2>
    <form id="customForm">
      <div class="row">
        <div>
          <label for="title">Title</label>
          <input id="title" name="title" required placeholder="e.g. DNA Extraction" />
        </div>
        <div>
          <label for="owner">Owner</label>
          <input id="owner" name="owner" required placeholder="e.g. Dr. Kim" />
        </div>
      </div>
      <div class="row">
        <div>
          <label for="minutes">Minutes</label>
          <input id="minutes" name="minutes" type="number" min="0" value="5" />
        </div>
        <div>
          <label for="seconds">Seconds</label>
          <input id="seconds" name="seconds" type="number" min="0" max="59" value="0" />
        </div>
      </div>
      <br />
      <button type="submit">Start Custom Timer</button>
    </form>
  </div>

  <div class="card">
    <h2>Presets</h2>
    <div class="row">
      <div>
        <label for="presetOwner">Owner for preset timers</label>
        <input id="presetOwner" placeholder="e.g. Team A" />
      </div>
      <div style="display:flex;align-items:flex-end;">
        <button id="togglePresets" type="button">Show Presets</button>
      </div>
    </div>
    <div id="presetList" style="display:none;margin-top:10px;"></div>
  </div>

  <div class="card">
    <h2>All Running Timers</h2>
    <div id="allTimers">No active timers.</div>
  </div>

  <div id="status"></div>

  <script>
    const statusEl = document.getElementById("status");
    const currentEl = document.getElementById("currentTimer");
    const allTimersEl = document.getElementById("allTimers");
    const presetListEl = document.getElementById("presetList");
    const togglePresetsBtn = document.getElementById("togglePresets");

    function showStatus(message, isError) {
      statusEl.textContent = message || "";
      statusEl.className = isError ? "error" : "";
      if (message) {
        setTimeout(() => {
          statusEl.textContent = "";
          statusEl.className = "";
        }, 2500);
      }
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, function (ch) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
      });
    }

    function formatTime(sec) {
      sec = Math.max(0, Number(sec || 0));
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      if (h > 0) {
        return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
      }
      return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
    }

    async function req(url, options) {
      const response = await fetch(url, options || {});
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Request failed");
      }
      return payload;
    }

    async function refreshState() {
      const data = await req("/api/state");
      if (data.current) {
        currentEl.innerHTML =
          "<b>" + escapeHtml(data.current.title) + "</b><br>" +
          "Owner: " + escapeHtml(data.current.owner) + "<br>" +
          "Time Remaining: " + formatTime(data.current.remainingSec);
      } else {
        currentEl.textContent = "No active timer.";
      }

      if (!data.timers || data.timers.length === 0) {
        allTimersEl.textContent = "No active timers.";
      } else {
        allTimersEl.innerHTML = data.timers.map(function (timer) {
          return "<div><b>" + escapeHtml(timer.title) + "</b> - Owner: " +
                 escapeHtml(timer.owner) + " - Remaining: " +
                 formatTime(timer.remainingSec) + "</div>";
        }).join("");
      }
    }

    async function loadPresets() {
      const presets = await req("/api/presets");
      presetListEl.innerHTML = presets.map(function (preset) {
        return '<div class="preset">' +
               '<b>' + escapeHtml(preset.title) + '</b><br>' +
               'Duration: ' + formatTime(preset.durationSec) + '<br><br>' +
               '<button data-id="' + preset.id + '">Start Preset</button>' +
               '</div>';
      }).join("");

      Array.from(presetListEl.querySelectorAll("button")).forEach(function (btn) {
        btn.addEventListener("click", async function () {
          try {
            const owner = (document.getElementById("presetOwner").value || "").trim() || "Unassigned";
            await req("/api/timer/preset", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                presetId: Number(btn.getAttribute("data-id")),
                owner: owner
              })
            });
            showStatus("Preset timer started.", false);
            await refreshState();
          } catch (err) {
            showStatus(err.message, true);
          }
        });
      });
    }

    document.getElementById("customForm").addEventListener("submit", async function (event) {
      event.preventDefault();
      try {
        const form = new FormData(event.target);
        const durationSec = Number(form.get("minutes") || 0) * 60 + Number(form.get("seconds") || 0);
        await req("/api/timer/custom", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: String(form.get("title") || ""),
            owner: String(form.get("owner") || ""),
            durationSec: durationSec
          })
        });
        event.target.reset();
        document.getElementById("minutes").value = 5;
        document.getElementById("seconds").value = 0;
        showStatus("Custom timer started.", false);
        await refreshState();
      } catch (err) {
        showStatus(err.message, true);
      }
    });

    togglePresetsBtn.addEventListener("click", async function () {
      if (presetListEl.style.display === "none") {
        presetListEl.style.display = "block";
        togglePresetsBtn.textContent = "Hide Presets";
        if (!presetListEl.dataset.loaded) {
          await loadPresets();
          presetListEl.dataset.loaded = "1";
        }
      } else {
        presetListEl.style.display = "none";
        togglePresetsBtn.textContent = "Show Presets";
      }
    });

    (async function boot() {
      try {
        await refreshState();
      } catch (err) {
        showStatus(err.message, true);
      }
      setInterval(async function () {
        try {
          await refreshState();
        } catch (err) {
          showStatus(err.message, true);
        }
      }, 1000);
    })();
  </script>
</body>
</html>
"""


def trim(value):
    if value is None:
        return ""
    return str(value).strip()


def duration_to_clock(total_sec):
    sec = int(total_sec)
    if sec < 0:
        sec = 0
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return "%02d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def is_pressed_edge(button_obj, key):
    if button_obj is None:
        return False
    if hasattr(button_obj, "wasPressed"):
        return bool(button_obj.wasPressed())
    if hasattr(button_obj, "isPressed"):
        pressed = bool(button_obj.isPressed())
        last = button_last_levels[key]
        button_last_levels[key] = pressed
        return pressed and not last
    return False


def remaining_ms(timer, now_ms):
    delta = utime.ticks_diff(timer["end_ms"], now_ms)
    if delta < 0:
        return 0
    return delta


def remaining_sec(timer, now_ms):
    return (remaining_ms(timer, now_ms) + 999) // 1000


def warning_window_sec(timer):
    window = timer["duration_sec"] // 6
    if window < 1:
        return 1
    return window


def expired_flash_active(now_ms):
    return user_must_clear_expired_alert


def timer_phase(current, now_ms):
    if expired_flash_active(now_ms):
        return "expired"
    if current is None:
        return "idle"
    if remaining_sec(current, now_ms) <= warning_window_sec(current):
        return "warning"
    return "running"


def trigger_expired_alert(expired_count):
    global user_must_clear_expired_alert
    if expired_count <= 0:
        return

    user_must_clear_expired_alert = True

    if speaker is not None and hasattr(speaker, "tone"):
        melody = [
            (1568, 150),
            (1319, 150),
            (1047, 500),
        ]
        for note in melody:
            frequency, duration = note
            speaker.tone(frequency, duration)
            utime.sleep_ms(55)


def prune_expired():
    global timers
    now_ms = utime.ticks_ms()
    active_timers = []
    expired_count = 0

    for timer in timers:
        if remaining_ms(timer, now_ms) > 0:
            active_timers.append(timer)
        else:
            expired_count += 1

    timers = active_timers
    if expired_count:
        trigger_expired_alert(expired_count)


def current_timer():
    prune_expired()
    if not timers:
        return None
    now_ms = utime.ticks_ms()
    best = timers[0]
    best_ms = remaining_ms(best, now_ms)
    for t in timers[1:]:
        t_ms = remaining_ms(t, now_ms)
        if t_ms < best_ms:
            best = t
            best_ms = t_ms
    return best


def serialize_timer(timer_obj):
    now_ms = utime.ticks_ms()
    return {
        "id": timer_obj["id"],
        "title": timer_obj["title"],
        "owner": timer_obj["owner"],
        "durationSec": timer_obj["duration_sec"],
        "remainingSec": remaining_sec(timer_obj, now_ms),
    }


def add_timer(title, owner, duration_sec):
    global next_timer_id

    title = trim(title)
    owner = trim(owner)
    duration_sec = int(duration_sec)

    if not title:
        return False, "Title cannot be empty."
    if not owner:
        return False, "Owner cannot be empty."
    if duration_sec <= 0 or duration_sec > MAX_TIMER_SEC:
        return False, "Duration must be between 1 second and 24 hours."

    start_ms = utime.ticks_ms()
    timers.append(
        {
            "id": next_timer_id,
            "title": title,
            "owner": owner,
            "duration_sec": duration_sec,
            "start_ms": start_ms,
            "end_ms": utime.ticks_add(start_ms, duration_sec * 1000),
        }
    )
    next_timer_id += 1

    if speaker is not None and hasattr(speaker, "tone"):
        speaker.tone(1400, 80)

    return True, ""


def list_timers_sorted():
    prune_expired()
    now_ms = utime.ticks_ms()
    sorted_timers = list(timers)
    sorted_timers.sort(key=lambda t: remaining_ms(t, now_ms))
    return sorted_timers


def json_state():
    sorted_timers = list_timers_sorted()
    current = current_timer()
    return {
        "current": serialize_timer(current) if current else None,
        "timers": [serialize_timer(t) for t in sorted_timers],
    }


def set_status(message):
    global status_message
    status_message = message


def reset_display_cache():
    global display_static_ready, display_cache, last_progress_fill, last_progress_color, last_render_phase
    display_static_ready = False
    display_cache = {}
    last_progress_fill = -1
    last_progress_color = None
    last_render_phase = None


def lcd_clear_screen():
    if not hasattr(lcd, "clear"):
        return
    try:
        lcd.clear(COLOR_BG)
    except TypeError:
        lcd.clear()


def lcd_print_at(x, y, message, color=COLOR_TEXT):
    text = str(message)
    if hasattr(lcd, "print"):
        try:
            lcd.print(text, x, y, color)
            return
        except TypeError:
            if hasattr(lcd, "setCursor"):
                lcd.setCursor(x, y)
                lcd.print(text)
                return
    if hasattr(lcd, "text"):
        try:
            lcd.text(x, y, text, color)
        except TypeError:
            lcd.text(x, y, text)


def lcd_fill_rect(x, y, w, h, color):
    if hasattr(lcd, "fillRect"):
        lcd.fillRect(x, y, w, h, color)
        return True
    if hasattr(lcd, "rect"):
        try:
            lcd.rect(x, y, w, h, color, color)
            return True
        except TypeError:
            return False
    return False


def lcd_draw_rect(x, y, w, h, border_color, fill_color=None):
    if fill_color is not None:
        lcd_fill_rect(x, y, w, h, fill_color)
    if hasattr(lcd, "rect"):
        try:
            lcd.rect(x, y, w, h, border_color)
        except TypeError:
            if fill_color is not None:
                lcd.rect(x, y, w, h, border_color, fill_color)


def lcd_fill_circle(x, y, radius, color):
    if hasattr(lcd, "circle"):
        try:
            lcd.circle(x, y, radius, color, color)
            return True
        except TypeError:
            try:
                lcd.circle(x, y, radius, color)
                return True
            except TypeError:
                return False
    return False


def lcd_update_text_field(field_id, x, y, w, h, text, fg, bg):
    key = (str(text), fg, bg)
    if display_cache.get(field_id) == key:
        return
    lcd_fill_rect(x, y, w, h, bg)
    lcd_print_at(x, y, text, fg)
    display_cache[field_id] = key


def draw_display_static(ip_address):
    lcd_clear_screen()
    lcd_fill_rect(0, 0, DISPLAY_WIDTH, 32, COLOR_HEADER)
    lcd_print_at(10, 8, "LAB TIMER", COLOR_TEXT)

    lcd_draw_rect(6, 38, 308, 146, COLOR_ACCENT, COLOR_PANEL)

    lcd_draw_rect(6, 190, 308, 44, COLOR_ACCENT, COLOR_PANEL)


def state_color(phase):
    if phase == "warning":
        return COLOR_WARNING
    if phase == "expired":
        return COLOR_EXPIRED
    if phase == "running":
        return COLOR_RUNNING
    return COLOR_ACCENT


def led_buffer_index(position):
    # LED_CLOCKWISE_MAP stores physical labels 1..10; convert to list index 0..9.
    return LED_CLOCKWISE_MAP[position] - 1


def get_battery_info():
    if power is None:
        return None, False

    percent = 0
    candidate_methods = (
        "getBatPercentage",
        "getBatteryLevel",
        "getBatteryPercentage",
        "getBatPercent",
    )

    for method_name in candidate_methods:
        if not hasattr(power, method_name):
            continue
        value = getattr(power, method_name)()
        try:
            p = int(value)
            if 0 <= p <= 100:
                percent = p
                break
        except (TypeError, ValueError):
            continue

    is_charging = False
    if hasattr(power, "isCharging"):
        try:
            is_charging = bool(power.isCharging())
        except:
            pass

    return percent, is_charging


def render_display_text_fallback(ip_address, current, now_ms, phase):
    lines = ["Lab Timer", "AP: %s" % ip_address, "Timers: %d" % len(timers)]
    if current is None:
        lines.append("No active timers")
    else:
        lines.append("Current: %s" % current["title"])
        lines.append("Owner: %s" % current["owner"])
        lines.append("Remain: %s" % duration_to_clock(remaining_sec(current, now_ms)))
        lines.append("State: %s" % phase.upper())

    if main_menu_open:
        item = MENU_ITEMS[main_menu_index]
        lines.append("Menu: %s" % item["title"])
        lines.append("A next / B close / C select")

    if status_message:
        lines.append(status_message)

    lcd_clear_screen()
    y = 0
    for line in lines:
        lcd_print_at(0, y, line)
        y += 16


def render_display(ip_address):
    global display_static_ready, last_progress_fill, last_progress_color, last_render_phase

    if lcd is None:
        return

    now_ms = utime.ticks_ms()
    current = current_timer()
    phase = timer_phase(current, now_ms)
    accent_color = state_color(phase)

    can_draw_boxes = hasattr(lcd, "rect") or hasattr(lcd, "fillRect")
    if not can_draw_boxes:
        render_display_text_fallback(ip_address, current, now_ms, phase)
        return

    if fullscreen_mode:
        if not display_static_ready:
            lcd_clear_screen()
            display_static_ready = True
            last_progress_fill = -1
            
        if current is None:
            lcd_update_text_field("fs_no_active", 0, 110, 320, 20, "No active timers", COLOR_MUTED, COLOR_BG)
        else:
            remain = remaining_sec(current, now_ms)
            lcd_update_text_field("fs_title", 0, 70, 320, 20, current["title"], COLOR_MUTED, COLOR_BG)
            lcd_update_text_field("fs_remain", 0, 100, 320, 40, "Remain: %s" % duration_to_clock(remain), accent_color, COLOR_BG)
            
            fill_w = 0
            if current["duration_sec"] > 0:
                fill_w = (300 * remain) // current["duration_sec"]
            if display_cache.get("fs_prog_border") != True:
                lcd_draw_rect(10, 160, 300, 20, COLOR_MUTED, COLOR_BG)
                display_cache["fs_prog_border"] = True
            if fill_w != last_progress_fill or accent_color != last_progress_color:
                lcd_fill_rect(11, 161, 298, 18, COLOR_BG)
                if fill_w > 0:
                    lcd_fill_rect(11, 161, fill_w, 18, accent_color)
                last_progress_fill = fill_w
                last_progress_color = accent_color
        return

    if not display_static_ready:
        draw_display_static(ip_address)
        display_static_ready = True

    battery_info = get_battery_info()
    battery_label = "Bat: [----]"
    if battery_info[0] is not None:
        percent, is_charging = battery_info
        bars = percent // 25
        if bars > 4: bars = 4
        if bars < 0: bars = 0
        
        bar_str = "#" * bars + "-" * (4 - bars)
        prefix = "Chg" if is_charging else "Bat"
        battery_label = "%s:[%s]" % (prefix, bar_str)
    lcd_update_text_field("header_ap", 200, 8, 95, 16, battery_label, COLOR_TEXT, COLOR_HEADER)

    if last_render_phase != phase:
        lcd_draw_rect(6, 38, 308, 146, accent_color, COLOR_PANEL)
        display_cache.pop("prog_border", None)
        footer_color = accent_color if status_message else COLOR_ACCENT
        lcd_draw_rect(6, 190, 308, 44, footer_color, COLOR_PANEL)
        display_cache.pop("footer_line1", None)
        display_cache.pop("footer_line2", None)
        last_render_phase = phase

    if current is None:
        if display_cache.get("prog_border") != False:
            lcd_fill_rect(14, 156, 292, 14, COLOR_PANEL)
            display_cache["prog_border"] = False
        lcd_update_text_field("title", 14, 70, 292, 16, "", COLOR_TEXT, COLOR_PANEL)
        lcd_update_text_field("owner", 14, 88, 292, 16, "", COLOR_MUTED, COLOR_PANEL)
        lcd_update_text_field("remain", 14, 108, 170, 16, "", accent_color, COLOR_PANEL)
        lcd_update_text_field("phase_tag", 188, 108, 118, 16, "", COLOR_MUTED, COLOR_PANEL)
        lcd_update_text_field("no_active", 14, 86, 292, 16, "No active timers", COLOR_MUTED, COLOR_PANEL)
        lcd_fill_rect(15, 157, 290, 12, COLOR_BG)
        last_progress_fill = 0
        last_progress_color = accent_color
    else:
        if display_cache.get("prog_border") != True:
            lcd_draw_rect(14, 156, 292, 14, COLOR_MUTED, COLOR_BG)
            display_cache["prog_border"] = True
        remain = remaining_sec(current, now_ms)
        lcd_update_text_field("no_active", 14, 86, 292, 16, "", COLOR_MUTED, COLOR_PANEL)
        lcd_update_text_field("title", 14, 70, 292, 16, current["title"], COLOR_TEXT, COLOR_PANEL)
        lcd_update_text_field("owner", 14, 88, 292, 16, "Owner: %s" % current["owner"], COLOR_MUTED, COLOR_PANEL)
        lcd_update_text_field(
            "remain",
            14,
            108,
            170,
            16,
            "Remaining: %s" % duration_to_clock(remain),
            accent_color,
            COLOR_PANEL,
        )

        phase_tag = ""
        if phase == "warning":
            phase_tag = "WARN"
        elif phase == "expired":
            phase_tag = "EXPIRED"
        lcd_update_text_field("phase_tag", 188, 108, 118, 16, phase_tag, accent_color, COLOR_PANEL)

        fill_w = 0
        if current["duration_sec"] > 0:
            fill_w = (290 * remain) // current["duration_sec"]
        if fill_w != last_progress_fill or accent_color != last_progress_color:
            lcd_fill_rect(15, 157, 290, 12, COLOR_BG)
            if fill_w > 0:
                lcd_fill_rect(15, 157, fill_w, 12, accent_color)
            last_progress_fill = fill_w
            last_progress_color = accent_color

    footer_color = accent_color if status_message else COLOR_ACCENT
    footer_state = (footer_color, status_message, main_menu_open, main_menu_index)
    if display_cache.get("footer_state") != footer_state:
        lcd_draw_rect(6, 190, 308, 44, footer_color, COLOR_PANEL)
        display_cache["footer_state"] = footer_state
        display_cache.pop("footer_line1", None)
        display_cache.pop("footer_line2", None)

    if main_menu_open:
        item = MENU_ITEMS[main_menu_index]
        lcd_update_text_field("footer_line1", 14, 198, 292, 16, "Menu: %s" % item["title"], COLOR_TEXT, COLOR_PANEL)
        lcd_update_text_field("footer_line2", 14, 214, 292, 16, "A next / B close / C select", COLOR_MUTED, COLOR_PANEL)
    elif status_message:
        lcd_update_text_field("footer_line1", 14, 198, 292, 16, "", COLOR_TEXT, COLOR_PANEL)
        lcd_update_text_field("footer_line2", 14, 206, 292, 16, status_message, COLOR_TEXT, COLOR_PANEL)
    else:
        lcd_update_text_field("footer_line1", 14, 198, 292, 16, "", COLOR_TEXT, COLOR_PANEL)
        lcd_update_text_field("footer_line2", 14, 206, 292, 16, "State: %s" % phase.upper(), COLOR_MUTED, COLOR_PANEL)


def apply_rgb_frame(colors):
    global last_rgb_frame
    if rgb is None:
        return

    frame = tuple(colors)
    if frame == last_rgb_frame:
        return

    if hasattr(rgb, "setColor"):
        for index, color in enumerate(colors):
            rgb.setColor(index, color)
    elif hasattr(rgb, "setColorAll"):
        fallback_color = RGB_OFF
        for color in colors:
            if color != RGB_OFF:
                fallback_color = color
                break
        rgb.setColorAll(fallback_color)
    else:
        return

    if hasattr(rgb, "show"):
        rgb.show()

    last_rgb_frame = frame


def compose_rgb_frame(current, now_ms, blink_on):
    phase = timer_phase(current, now_ms)
    colors = [RGB_OFF] * LED_COUNT

    if phase == "idle":
        return colors

    if phase == "expired":
        fill_color = RGB_EXPIRED if blink_on else RGB_FLASH
        for pos in range(LED_COUNT):
            colors[led_buffer_index(pos)] = fill_color
        return colors
        
    if phase == "warning":
        fill_color = RGB_WARNING if blink_on else RGB_OFF
        for pos in range(LED_COUNT):
            colors[led_buffer_index(pos)] = fill_color
        return colors

    remaining = remaining_sec(current, now_ms)
    duration = current["duration_sec"]
    remaining_ratio = remaining / float(duration) if duration > 0 else 0
    if remaining_ratio < 0:
        remaining_ratio = 0
    if remaining_ratio > 1:
        remaining_ratio = 1

    if remaining_ratio > 5/6: lit_rows = 5
    elif remaining_ratio > 4/6: lit_rows = 4
    elif remaining_ratio > 3/6: lit_rows = 3
    elif remaining_ratio > 2/6: lit_rows = 2
    elif remaining_ratio > 1/6: lit_rows = 1
    else: lit_rows = 0

    total_rows = len(LED_SYMMETRIC_ROWS)
    active_color = RGB_RUNNING

    # Drain toward the bottom: top rows turn off first as time decreases.
    first_lit_row = total_rows - lit_rows
    for row_index, pair in enumerate(LED_SYMMETRIC_ROWS):
        right_label, left_label = pair
        right_index = right_label - 1
        left_index = left_label - 1
        should_light = row_index >= first_lit_row
        if should_light:
            colors[right_index] = active_color
            colors[left_index] = active_color
        else:
            colors[right_index] = RGB_OFF
            colors[left_index] = RGB_OFF

    return colors


def update_rgb_feedback():
    global last_rgb_ms, rgb_blink_phase
    if rgb is None:
        return

    now_ms = utime.ticks_ms()
    if utime.ticks_diff(now_ms, last_rgb_ms) < RGB_REFRESH_MS:
        return

    last_rgb_ms = now_ms
    rgb_blink_phase = not rgb_blink_phase
    current = current_timer()
    apply_rgb_frame(compose_rgb_frame(current, now_ms, rgb_blink_phase))


def handle_buttons(ip_address):
    global main_menu_open, main_menu_index, preset_last_activity_ms, fullscreen_mode, user_must_clear_expired_alert

    now_ms = utime.ticks_ms()

    if user_must_clear_expired_alert:
        if is_pressed_edge(btnA, "A") or is_pressed_edge(btnB, "B") or is_pressed_edge(btnC, "C"):
            user_must_clear_expired_alert = False
            set_status("Alarm cleared")
        return

    if fullscreen_mode:
        if is_pressed_edge(btnA, "A") or is_pressed_edge(btnB, "B") or is_pressed_edge(btnC, "C"):
            fullscreen_mode = False
            reset_display_cache()
            set_status("Exited fullscreen")
        return

    if is_pressed_edge(btnB, "B"):
        main_menu_open = not main_menu_open
        if main_menu_open:
            preset_last_activity_ms = now_ms
            set_status("Menu opened")
        else:
            set_status("Menu closed")

    if main_menu_open and utime.ticks_diff(now_ms, preset_last_activity_ms) >= PRESET_MENU_TIMEOUT_MS:
        main_menu_open = False
        set_status("Menu closed (idle)")

    if not main_menu_open:
        return

    if is_pressed_edge(btnA, "A"):
        preset_last_activity_ms = now_ms
        main_menu_index = (main_menu_index + 1) % len(MENU_ITEMS)
        set_status("Menu: %s" % MENU_ITEMS[main_menu_index]["title"])

    if is_pressed_edge(btnC, "C"):
        preset_last_activity_ms = now_ms
        item = MENU_ITEMS[main_menu_index]
        if item["type"] == "preset":
            selected = item["data"]
            ok, message = add_timer(selected["title"], "Mar Roca", selected["duration_sec"])
            if ok:
                set_status("Started: %s" % selected["title"])
                main_menu_open = False
            else:
                set_status(message)
        elif item["type"] == "action":
            if item["action"] == "show_ip":
                set_status("IP: %s" % ip_address)
            elif item["action"] == "toggle_fs":
                fullscreen_mode = True
                reset_display_cache()
                main_menu_open = False
                set_status("Fullscreen active")


def send_response(client, status_code, content_type, body):
    status_text = {
        200: "OK",
        201: "Created",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
    }.get(status_code, "OK")

    if isinstance(body, str):
        body = body.encode("utf-8")

    header = (
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "Connection: close\r\n\r\n"
    ) % (status_code, status_text, content_type, len(body))

    client.send(header.encode("utf-8"))
    client.send(body)


def send_json(client, status_code, payload):
    send_response(client, status_code, "application/json; charset=utf-8", ujson.dumps(payload))


def parse_request(client):
    raw = b""
    while True:
        chunk = client.recv(1024)
        if not chunk:
            break
        raw += chunk
        if b"\r\n\r\n" in raw:
            break
        if len(raw) > 16384:
            break

    if not raw:
        return None

    head, _, body = raw.partition(b"\r\n\r\n")
    head_text = head.decode("utf-8", "ignore")
    lines = head_text.split("\r\n")
    if not lines:
        return None

    first_line = lines[0].split(" ")
    if len(first_line) < 2:
        return None

    method = first_line[0]
    path = first_line[1].split("?")[0]

    content_length = 0
    for line in lines[1:]:
        lower = line.lower()
        if lower.startswith("content-length:"):
            value = lower.split(":", 1)[1].strip()
            if value.isdigit():
                content_length = int(value)
            break

    while len(body) < content_length:
        chunk = client.recv(1024)
        if not chunk:
            break
        body += chunk

    body_text = body.decode("utf-8", "ignore")
    return method, path, body_text


def find_preset(preset_id):
    for preset in PRESETS:
        if preset["id"] == preset_id:
            return preset
    return None


def handle_client(client):
    try:
        parsed = parse_request(client)
        if parsed is None:
            send_json(client, 400, {"error": "Invalid HTTP request."})
            return

        method, path, body_text = parsed

        if method == "GET" and path == "/":
            send_response(client, 200, "text/html; charset=utf-8", INDEX_HTML)
            return

        if method == "GET" and path == "/api/presets":
            payload = []
            for p in PRESETS:
                payload.append(
                    {
                        "id": p["id"],
                        "title": p["title"],
                        "durationSec": p["duration_sec"],
                    }
                )
            send_json(client, 200, payload)
            return

        if method == "GET" and path == "/api/state":
            send_json(client, 200, json_state())
            return

        if method == "POST" and path == "/api/timer/custom":
            try:
                payload = ujson.loads(body_text or "{}")
            except ValueError:
                send_json(client, 400, {"error": "Invalid JSON payload."})
                return

            ok, message = add_timer(
                payload.get("title", ""),
                payload.get("owner", ""),
                payload.get("durationSec", 0),
            )
            if not ok:
                send_json(client, 400, {"error": message})
                return
            send_json(client, 201, {"ok": True})
            return

        if method == "POST" and path == "/api/timer/preset":
            try:
                payload = ujson.loads(body_text or "{}")
            except ValueError:
                send_json(client, 400, {"error": "Invalid JSON payload."})
                return

            preset = find_preset(int(payload.get("presetId", -1)))
            if preset is None:
                send_json(client, 404, {"error": "Preset not found."})
                return

            owner = trim(payload.get("owner", "")) or "Unassigned"
            title = trim(payload.get("title", "")) or preset["title"]
            ok, message = add_timer(title, owner, preset["duration_sec"])
            if not ok:
                send_json(client, 400, {"error": message})
                return
            send_json(client, 201, {"ok": True})
            return

        if method not in ("GET", "POST"):
            send_json(client, 405, {"error": "Method not allowed."})
            return

        send_json(client, 404, {"error": "Not found."})
    finally:
        client.close()


def connect_network():
    sta = network.WLAN(network.STA_IF)
    if sta.active():
        sta.active(False)

    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID, password=AP_PASSWORD)
    return ap.ifconfig()[0], "ap"


def run_server():
    global last_display_ms

    ip_address, mode = connect_network()
    set_status("WiFi mode: %s" % mode.upper())
    reset_display_cache()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", HTTP_PORT))
    server.listen(2)
    server.setblocking(False)

    poller = uselect.poll()
    poller.register(server, uselect.POLLIN)

    set_status("Web: http://%s" % ip_address)
    render_display(ip_address)

    while True:
        prune_expired()
        handle_buttons(ip_address)
        update_rgb_feedback()

        now_ms = utime.ticks_ms()
        if utime.ticks_diff(now_ms, last_display_ms) >= DISPLAY_REFRESH_MS:
            last_display_ms = now_ms
            render_display(ip_address)

        events = poller.poll(50)
        if not events:
            continue

        for entry in events:
            sock_obj = entry[0]
            if sock_obj is not server:
                continue
            client, _ = server.accept()
            client.setblocking(True)
            handle_client(client)


run_server()