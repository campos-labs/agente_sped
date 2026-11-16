"use strict";

let rememberedPeriod;

function updateAnalysisFields(form) {
  const selection = form.querySelector("[name='analysis_id']");
  for (const field of form.querySelectorAll("[data-variants]")) {
    const input = field.querySelector("input");
    const variant = JSON.parse(field.dataset.variants)[selection.value];
    field.hidden = !variant || input.type === "hidden";
    input.disabled = !variant || selection.disabled;
    if (!variant) continue;

    input.value = variant.value;
    if (input.type === "hidden") continue;
    input.required = variant.required;
    for (const name of ["min", "max", "step"]) {
      const value = variant[name];
      if (value === null) input.removeAttribute(name);
      else input.setAttribute(name, value);
    }
    field.querySelector("label").textContent = variant.label;
  }
  form.querySelector("[data-full-period]").hidden =
    selection.selectedOptions[0]?.dataset.periodScope !== "full_file";
}

function restorePeriod() {
  const period = document.querySelector("[data-source-period]");
  if (!period) {
    if (!document.querySelector("#workspace[aria-busy='true']")) rememberedPeriod = undefined;
    return;
  }
  if (rememberedPeriod?.source !== period.dataset.sourcePeriod) return;
  for (const input of period.querySelectorAll("input")) {
    input.value = rememberedPeriod.values[input.name];
  }
}

function applyPeriod(form) {
  const fields = Array.from(form.querySelectorAll("[data-period-field]:not(:disabled)"));
  const period = document.querySelector("[data-source-period]");
  if (!fields.length || !period) return true;

  const inputs = Array.from(period.querySelectorAll("input"));
  const [start, end] = inputs;
  end.setCustomValidity(start.value && end.value && start.value > end.value ?
    "A data final deve ser igual ou posterior à inicial." : "");
  for (const input of inputs) {
    if (!input.reportValidity()) return false;
  }
  for (const field of fields) {
    const source = inputs.find((input) => input.name === field.name);
    if (source) field.value = source.value;
  }
  return true;
}

document.addEventListener("input", (event) => {
  const period = event.target.closest("[data-source-period]");
  if (!period) return;
  const inputs = Array.from(period.querySelectorAll("input"));
  for (const input of inputs) input.setCustomValidity("");
  rememberedPeriod = {
    source: period.dataset.sourcePeriod,
    values: Object.fromEntries(inputs.map((input) => [input.name, input.value])),
  };
});

document.addEventListener("change", (event) => {
  if (!event.target.matches("select[name='analysis_id']")) return;
  const form = event.target.closest("[data-analysis-form]");
  if (form) updateAnalysisFields(form);
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  const confirmation = form.dataset.confirm;
  if ((confirmation && !window.confirm(confirmation)) || !applyPeriod(form)) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

document.addEventListener("htmx:after:swap", restorePeriod);
