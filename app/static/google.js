// "Continue with Google" (Google Identity Services). Google gives the browser a signed ID token;
// we post it to the server, which checks it with Google before signing anyone in.
//   <div data-google-signin data-client-id="…" data-endpoint="/account/google" data-next="/account"></div>
(function () {
  const el = document.querySelector("[data-google-signin]");
  if (!el) return;

  function post(credential) {
    const form = Object.assign(document.createElement("form"), { method: "post", action: el.dataset.endpoint });
    for (const [name, value] of [["credential", credential], ["next", el.dataset.next || ""]]) {
      form.append(Object.assign(document.createElement("input"), { type: "hidden", name, value }));
    }
    document.body.append(form);
    form.submit();
  }

  const script = Object.assign(document.createElement("script"), { src: "https://accounts.google.com/gsi/client", async: true });
  script.onload = () => {
    google.accounts.id.initialize({ client_id: el.dataset.clientId, callback: (r) => post(r.credential) });
    google.accounts.id.renderButton(el, {
      theme: "outline", size: "large", shape: "pill", text: "continue_with", width: Math.min(el.offsetWidth || 320, 400),
    });
    el.closest("[data-google-block]")?.removeAttribute("hidden");
  };
  document.head.append(script);
})();
