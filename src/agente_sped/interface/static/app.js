"use strict";

function updateAnalysisFields(form) {
  const selection = form.querySelector("[name='analysis_id']");
  if (!selection) return;
  for (const field of form.querySelectorAll("[data-analyses]")) {
    const active = field.dataset.analyses.split(" ").includes(selection.value);
    field.hidden = !active;
    const variant = JSON.parse(field.dataset.variants)[selection.value];
    for (const input of field.querySelectorAll("input")) {
      input.disabled = !active || selection.disabled;
      if (!active || !variant) continue;
      input.type = variant.type === "date" ? "date" : "number";
      input.value = variant.value;
      input.required = variant.required;
      for (const name of ["min", "max", "step"]) {
        const value = variant[name];
        if (value === null || variant.type === "date" && name === "step") {
          input.removeAttribute(name);
        } else {
          input.setAttribute(name, value);
        }
      }
      field.querySelector("label").textContent = variant.label;
    }
  }
  const note = form.querySelector("[data-full-period]");
  if (note) {
    note.hidden = selection.selectedOptions[0]?.dataset.periodScope !== "full_file";
  }
}

document.addEventListener("change", (event) => {
  if (!event.target.matches("select[name='analysis_id']")) return;
  const form = event.target.closest("[data-analysis-form]");
  if (form) updateAnalysisFields(form);
});

document.addEventListener("submit", (event) => {
  const question = event.target.dataset.confirm;
  if (question && !window.confirm(question)) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

function initializeForms() {
  for (const form of document.querySelectorAll("[data-analysis-form]")) {
    updateAnalysisFields(form);
  }
}

document.addEventListener("DOMContentLoaded", initializeForms);
document.addEventListener("htmx:after:swap", initializeForms);
