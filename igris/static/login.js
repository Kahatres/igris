(function () {
  "use strict";
  var form = document.getElementById("login-form");
  var error = document.getElementById("login-error");
  var submit = document.getElementById("login-submit");
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (submit.disabled) { return; }
    submit.disabled = true;
    error.textContent = "";
    fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.elements.username.value,
                             password: form.elements.password.value })
    }).then(function (response) {
      form.elements.password.value = "";
      if (response.ok) { window.location.replace("/"); return; }
      error.textContent = response.status === 429
        ? "Çok fazla giriş denemesi yapıldı. Lütfen 15 dakika sonra tekrar deneyin."
        : "Giriş yapılamadı. Kullanıcı adınızı ve parolanızı kontrol edin.";
    }).catch(function () {
      error.textContent = "Sunucuya ulaşılamadı. Lütfen tekrar deneyin.";
    }).then(function () { submit.disabled = false; });
  });
})();
