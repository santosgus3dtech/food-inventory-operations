document.querySelectorAll("[data-password-toggle]").forEach((button) => {
  const input = document.getElementById(button.getAttribute("aria-controls"));
  if (!input) return;

  button.addEventListener("click", () => {
    const willShow = input.type === "password";
    input.type = willShow ? "text" : "password";
    button.textContent = willShow ? "Ocultar" : "Mostrar";
    button.setAttribute("aria-pressed", String(willShow));
    input.focus({ preventScroll: true });
  });
});
