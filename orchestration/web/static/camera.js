/* Camera capture for the Pod 12 console: snap photos with the laptop or phone camera, store them with the same upload
   route a file upload uses (same size/type checks on the server), then optionally run that step's real agent at once.
   Works on http://localhost (a secure context). Nothing is sent anywhere except this console. */
(function () {
  "use strict";
  var modal, video, canvas, shots, status, stream = null, ctx = null, pics = [];

  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (html) e.innerHTML = html;
    return e;
  }

  function build() {
    if (modal) return;
    modal = el("div", { id: "cam", role: "dialog", "aria-modal": "true", "aria-labelledby": "cam-title", hidden: "" });
    modal.innerHTML =
      '<div class="cam-box">' +
      '<div class="cam-head"><h2 id="cam-title">Take a photo</h2><button type="button" class="btn alt small" data-cam="close">✕ Close</button></div>' +
      '<p class="muted small" id="cam-hint"></p>' +
      '<div class="cam-stage"><video playsinline muted autoplay></video><div class="cam-off">Starting the camera…</div></div>' +
      '<div class="cam-shots"></div>' +
      '<div class="cam-actions">' +
      '<button type="button" class="btn" data-cam="snap">📸 Snap</button>' +
      '<label class="btn alt"><input type="file" accept="image/jpeg,image/png,image/webp" capture="environment" multiple hidden data-cam="file">📁 Or pick a file</label>' +
      '<select data-cam="device" class="cam-device" aria-label="Camera" hidden></select>' +
      '<span class="spacer"></span>' +
      '<button type="button" class="btn" data-cam="send" disabled>✔ Save</button>' +
      "</div>" +
      '<p class="cam-status small" aria-live="polite"></p>' +
      "</div>";
    document.body.appendChild(modal);
    video = modal.querySelector("video");
    canvas = document.createElement("canvas");
    shots = modal.querySelector(".cam-shots");
    status = modal.querySelector(".cam-status");
    modal.addEventListener("click", function (e) {
      var a = e.target.closest("[data-cam]");
      if (e.target === modal) return close();
      if (!a) return;
      var what = a.getAttribute("data-cam");
      if (what === "close") close();
      if (what === "snap") snap();
      if (what === "send") send();
    });
    modal.querySelector("[data-cam=file]").addEventListener("change", function (e) {
      Array.prototype.forEach.call(e.target.files, function (f) { add(f, URL.createObjectURL(f)); });
      e.target.value = "";
    });
    modal.querySelector("[data-cam=device]").addEventListener("change", function (e) { start(e.target.value); });
    document.addEventListener("keydown", function (e) {
      if (modal.hidden) return;
      if (e.key === "Escape") close();
      if (e.key === " " && document.activeElement.tagName !== "SELECT") { e.preventDefault(); snap(); }
    });
  }

  function stop() {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
  }

  function start(deviceId) {
    stop();
    var off = modal.querySelector(".cam-off");
    off.textContent = "Starting the camera…";
    off.hidden = false;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      off.textContent = "This browser cannot open a camera here. Use “Or pick a file”.";
      return;
    }
    var v = deviceId ? { deviceId: { exact: deviceId } } : { facingMode: "environment" };
    v.width = { ideal: 1920 };
    v.height = { ideal: 1080 };
    navigator.mediaDevices.getUserMedia({ video: v, audio: false }).then(function (s) {
      stream = s;
      video.srcObject = s;
      off.hidden = true;
      return navigator.mediaDevices.enumerateDevices();
    }).then(function (devs) {
      if (!devs) return;
      var sel = modal.querySelector("[data-cam=device]"), cur = stream && stream.getVideoTracks()[0].getSettings().deviceId;
      var cams = devs.filter(function (d) { return d.kind === "videoinput"; });
      sel.innerHTML = cams.map(function (d, i) {
        return '<option value="' + d.deviceId + '"' + (d.deviceId === cur ? " selected" : "") + ">" +
          (d.label || "Camera " + (i + 1)).replace(/</g, "&lt;") + "</option>";
      }).join("");
      sel.hidden = cams.length < 2;
    }).catch(function (err) {
      off.textContent = "No camera: " + (err && err.name === "NotAllowedError" ? "permission was refused." : (err && err.message) || err) +
        " Use “Or pick a file”.";
    });
  }

  function room() { return ctx.max - pics.length; }

  function add(blob, url) {
    if (room() <= 0) { status.textContent = "This step takes at most " + ctx.max + " photo(s)."; return; }
    pics.push(blob);
    var i = pics.length - 1, fig = el("figure", {}, '<img alt="photo ' + (i + 1) + '"><button type="button" title="Remove">✕</button>');
    fig.querySelector("img").src = url;
    fig.querySelector("button").addEventListener("click", function () { pics[i] = null; fig.remove(); refresh(); });
    shots.appendChild(fig);
    refresh();
  }

  function refresh() {
    var n = pics.filter(Boolean).length;
    var send = modal.querySelector("[data-cam=send]");
    send.disabled = n === 0;
    send.textContent = ctx.run ? "✔ Save " + n + " & run " + ctx.label : "✔ Save " + n + " photo" + (n === 1 ? "" : "s");
    status.textContent = n ? n + " of up to " + ctx.max + " ready." : "";
  }

  function snap() {
    if (!stream || !video.videoWidth) { status.textContent = "The camera is not ready yet."; return; }
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    modal.querySelector(".cam-stage").classList.remove("flash");
    void modal.offsetWidth;
    modal.querySelector(".cam-stage").classList.add("flash");
    canvas.toBlob(function (b) { if (b) add(b, URL.createObjectURL(b)); }, "image/jpeg", 0.92);
  }

  function send() {
    var list = pics.filter(Boolean);
    if (!list.length) return;
    var fd = new FormData();
    list.forEach(function (b, i) {
      var ext = b.type === "image/png" ? ".png" : b.type === "image/webp" ? ".webp" : ".jpg";
      fd.append("files", b, (b.name && /\.(jpe?g|png|webp)$/i.test(b.name)) ? b.name : "camera-" + (i + 1) + ext);
    });
    modal.querySelector("[data-cam=send]").disabled = true;
    status.textContent = "Saving…";
    fetch(ctx.upload, { method: "POST", body: fd, credentials: "same-origin" }).then(function (r) {
      var u = new URL(r.url, location.href);
      if (!r.ok || u.searchParams.get("bad") === "1") {
        throw new Error(u.searchParams.get("msg") || ("the server answered " + r.status));
      }
      stop();
      if (ctx.run) {
        status.textContent = "Saved. Running " + ctx.label + "…";
        var f = document.querySelector(ctx.run);
        if (f) { f.dispatchEvent(new Event("submit", { cancelable: true })); f.submit(); return; }
      }
      location.reload();
    }).catch(function (err) {
      status.textContent = "Not saved: " + err.message;
      modal.querySelector("[data-cam=send]").disabled = false;
    });
  }

  function open(btn) {
    build();
    ctx = {
      upload: btn.getAttribute("data-camera"),
      max: parseInt(btn.getAttribute("data-max") || "6", 10),
      run: btn.getAttribute("data-run") || "",
      label: btn.getAttribute("data-label") || "this step",
      hint: btn.getAttribute("data-hint") || "",
    };
    pics = [];
    shots.innerHTML = "";
    modal.querySelector("#cam-title").textContent = btn.getAttribute("data-title") || "Take a photo";
    modal.querySelector("#cam-hint").textContent = ctx.hint;
    modal.hidden = false;
    document.documentElement.classList.add("cam-open");
    refresh();
    start();
  }

  function close() {
    stop();
    if (modal) modal.hidden = true;
    document.documentElement.classList.remove("cam-open");
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-camera]");
    if (!b) return;
    e.preventDefault();
    open(b);
  });
})();
