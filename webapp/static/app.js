// Add / remove rows in the answer key editor, and confirm destructive actions.
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("click", function (e) {
  var rm = e.target.closest("[data-remove-row]");
  if (rm) { rm.closest(".qrow").remove(); return; }
  if (e.target.id === "add-row") {
    var tpl = document.getElementById("row-template");
    var list = document.getElementById("rows");
    var clone = tpl.content.cloneNode(true);
    var max = 0;
    list.querySelectorAll("input[name=number]").forEach(function (i) { max = Math.max(max, parseInt(i.value, 10) || 0); });
    clone.querySelector("input[name=number]").value = max + 1;
    list.appendChild(clone);
    list.lastElementChild.querySelector("textarea").focus();
  }
});
