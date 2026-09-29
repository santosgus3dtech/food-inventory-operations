(() => {
  const form = document.querySelector("#fiscal-review-form");
  const confirmButton = document.querySelector("#confirm-fiscal");
  const deleteDraftButton = document.querySelector("[data-delete-draft]");
  deleteDraftButton?.addEventListener("click", (event) => {
    const confirmed = window.confirm(
      "Excluir o rascunho salvo? O XML continuará disponível e o estoque não será alterado.",
    );
    if (!confirmed) event.preventDefault();
  });
  if (!form || !confirmButton) return;

  const pnaeFoodIds = new Set(
    JSON.parse(document.querySelector("#pnae-food-ids")?.textContent || "[]").map(String),
  );

  const parseQuantity = (input) => {
    const value = input.value.trim().replace(",", ".");
    if (!value) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };

  const formatQuantity = (value) =>
    value.toLocaleString("pt-BR", { maximumFractionDigits: 3, minimumFractionDigits: 0 });

  const update = () => {
    let allComplete = true;
    document.querySelectorAll("[data-allocation-row]").forEach((row) => {
      const totalInput = row.querySelector(".allocation-total");
      const allocationInputs = [...row.querySelectorAll(".allocation-quantity")];
      const foodSelect = row.querySelector("select[name$='-food']");
      const pnaeCompliant = row.querySelector("[data-pnae-compliant]");
      const pnaeException = row.querySelector("[data-pnae-exception]");
      const exceptionStatus = row.querySelector("[data-pnae-exception-food]");
      const output = row.querySelector(".allocation-remaining");
      const total = parseQuantity(totalInput);
      const allocated = allocationInputs.reduce(
        (sum, input) => sum + (parseQuantity(input) || 0),
        0,
      );
      const selectedFood = foodSelect?.value || "";
      const outsidePnae = Boolean(selectedFood && pnaeFoodIds.size && !pnaeFoodIds.has(selectedFood));
      if (pnaeCompliant) pnaeCompliant.hidden = !selectedFood || outsidePnae;
      if (pnaeException) pnaeException.hidden = !outsidePnae;
      const exceptionApproved = Boolean(
        exceptionStatus?.classList.contains("approved")
          && exceptionStatus.dataset.pnaeExceptionFood === selectedFood,
      );
      if (outsidePnae && !exceptionApproved) allComplete = false;

      totalInput.setCustomValidity("");
      row.classList.remove("allocation-complete", "allocation-pending", "allocation-over");
      if (total === null || total <= 0) {
        output.textContent = "Informe o total";
        output.className = "allocation-remaining pending";
        row.classList.add("allocation-pending");
        allComplete = false;
        return;
      }

      const remaining = Math.round((total - allocated) * 1000) / 1000;
      if (remaining < 0) {
        output.textContent = `${formatQuantity(Math.abs(remaining))} acima`;
        output.className = "allocation-remaining over";
        row.classList.add("allocation-over");
        totalInput.setCustomValidity("A distribuição ultrapassa o total deste item.");
        allComplete = false;
      } else if (remaining > 0) {
        output.textContent = `${formatQuantity(remaining)} restante`;
        output.className = "allocation-remaining pending";
        row.classList.add("allocation-pending");
        allComplete = false;
      } else {
        output.textContent = "Completo";
        output.className = "allocation-remaining complete";
        row.classList.add("allocation-complete");
      }
      if (!foodSelect || !selectedFood || allocated <= 0) allComplete = false;
    });
    confirmButton.disabled = !allComplete;
    confirmButton.title = allComplete
      ? "Registrar todas as entradas"
      : "Identifique e distribua completamente todos os itens";
  };

  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
})();
