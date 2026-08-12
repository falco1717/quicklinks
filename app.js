const GENERAL_KEY = "general";
const THEME_KEY = "quicklinks-theme";
const DEFAULT_LOGO = "/assets/quicklinks-logo.png";
const DEFAULT_DARK_LOGO = "/assets/quicklinks-logo-dark.png";

const state = {
  locations: [],
  links: [],
  departments: [],
  viewer: {},
  branding: {},
  product: {
    notice: "QuickLinks · Created by Jordan Farmer"
  }
};

const elements = {
  brandLogo: document.querySelector("#brandLogo"),
  departmentField: document.querySelector("#departmentField"),
  departmentSelect: document.querySelector("#departmentSelect"),
  viewerStrip: document.querySelector("#viewerStrip"),
  viewerName: document.querySelector("#viewerName"),
  signOutButton: document.querySelector("#signOutButton"),
  portalLogin: document.querySelector("#portalLogin"),
  portalBody: document.querySelector("#portalBody"),
  portalLoginForm: document.querySelector("#portalLoginForm"),
  portalUsername: document.querySelector("#portalUsername"),
  portalPassword: document.querySelector("#portalPassword"),
  portalLoginError: document.querySelector("#portalLoginError"),
  portalEntra: document.querySelector("#portalEntra"),
  portalEntraButton: document.querySelector("#portalEntraButton"),
  companyName: document.querySelector("#companyName"),
  departmentTitle: document.querySelector("#departmentTitle"),
  locationSelect: document.querySelector("#locationSelect"),
  searchInput: document.querySelector("#searchInput"),
  themeToggle: document.querySelector("#themeToggle"),
  themeLabel: document.querySelector("#themeLabel"),
  viewMeta: document.querySelector("#viewMeta"),
  viewTitle: document.querySelector("#viewTitle"),
  resultCount: document.querySelector("#resultCount"),
  content: document.querySelector("#content"),
  productNotice: document.querySelector("#productNotice")
};

function applyProductNotice(product = {}) {
  state.product = { ...state.product, ...product };
  elements.productNotice.textContent = state.product.notice;
}

function applyBranding(branding = {}) {
  const companyName = branding.company_name || "QuickLinks";
  const departmentTitle = branding.department_title || "Link Portal";
  state.branding = branding;
  elements.companyName.textContent = companyName;
  elements.departmentTitle.textContent = departmentTitle;
  elements.brandLogo.alt = companyName;
  document.title = `${companyName} ${departmentTitle}`;
  updateBrandLogo();
}

function isDefaultLogo(logoUrl = "") {
  return !logoUrl || logoUrl === DEFAULT_LOGO || logoUrl.endsWith(DEFAULT_LOGO);
}

function updateBrandLogo() {
  const logoUrl = state.branding.logo_url || DEFAULT_LOGO;
  const isDark = document.documentElement.dataset.theme === "dark";
  elements.brandLogo.src = isDark && isDefaultLogo(logoUrl) ? DEFAULT_DARK_LOGO : logoUrl;
}

function selectedDepartmentId() {
  return Number(elements.departmentSelect.value) || null;
}

function departmentsAvailable() {
  return state.departments.length > 0;
}

// Locations and general links both belong to a department, so everything the
// page renders is filtered to the one on screen.
function departmentLocations() {
  const departmentId = selectedDepartmentId();
  if (!departmentId) return state.locations;
  return state.locations.filter((location) => location.department_id === departmentId);
}

function departmentLinks() {
  const departmentId = selectedDepartmentId();
  if (!departmentId) return state.links;
  return state.links.filter((link) => link.department_id === departmentId);
}

function populateDepartments() {
  elements.departmentSelect.replaceChildren();
  state.departments.forEach((department) => {
    const option = document.createElement("option");
    option.value = String(department.id);
    option.textContent = department.name;
    elements.departmentSelect.append(option);
  });
  // With a single department there is no choice to make, so the selector only
  // appears once it would actually do something.
  elements.departmentField.classList.toggle("hidden", state.departments.length < 2);
}

function selectDepartmentFromUrl() {
  const requested = new URLSearchParams(window.location.search).get("department");
  const match = state.departments.find((department) => department.slug === requested);
  const chosen = match || state.departments[0];
  if (chosen) elements.departmentSelect.value = String(chosen.id);
}

function currentDepartment() {
  const departmentId = selectedDepartmentId();
  return state.departments.find((department) => department.id === departmentId) || null;
}

function groupBy(items, key) {
  return items.reduce((groups, item) => {
    const groupName = item[key] || "General";
    groups[groupName] = groups[groupName] || [];
    groups[groupName].push(item);
    return groups;
  }, {});
}

function compareByName(a, b) {
  return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
}

function compareByTitle([titleA], [titleB]) {
  if (titleA === "General") return -1;
  if (titleB === "General") return 1;
  return titleA.localeCompare(titleB, undefined, { sensitivity: "base" });
}

function matchesSearch(item, query) {
  if (!query) return true;
  return [item.name, item.url, item.group_name, item.cluster, item.location_name, item.description]
    .filter(Boolean)
    .some((value) => value.toLowerCase().includes(query));
}

function createLinkCard(item) {
  const shell = document.createElement("div");
  shell.className = "link-card-shell";

  const link = document.createElement("a");
  link.className = "link-card";
  link.href = item.url;
  link.target = "_blank";
  link.rel = "noreferrer";

  const title = document.createElement("span");
  title.className = "link-title";
  title.textContent = item.name;

  const description = document.createElement("span");
  description.className = "link-description";
  description.textContent = item.description || item.cluster || "Open this service.";

  link.append(title, description);

  if (item.cluster && item.cluster !== description.textContent) {
    const context = document.createElement("span");
    context.className = "link-context";
    context.textContent = item.cluster;
    link.append(context);
  }

  const copyButton = document.createElement("button");
  copyButton.className = "copy-button";
  copyButton.type = "button";
  copyButton.setAttribute("aria-label", `Copy URL for ${item.name}`);
  copyButton.title = "Copy URL";
  copyButton.innerHTML = `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="9" y="9" width="10" height="10" rx="2"></rect>
      <path d="M5 15V7a2 2 0 0 1 2-2h8"></path>
    </svg>
  `;
  copyButton.addEventListener("click", () => copyUrl(item.url, item.name));

  shell.append(link, copyButton);
  return shell;
}

function createGroup(title, items, note) {
  const section = document.createElement("section");
  section.className = "group-section";

  const header = document.createElement("div");
  header.className = "group-header";

  const heading = document.createElement("h3");
  heading.textContent = title;

  const meta = document.createElement("p");
  meta.textContent = note || `${items.length} links`;

  const grid = document.createElement("div");
  grid.className = "links-grid";
  items.forEach((item) => grid.append(createLinkCard(item)));

  header.append(heading, meta);
  section.append(header, grid);
  return section;
}

function createEmptyState(message) {
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent = message;
  return empty;
}

function allVisibleLinks() {
  const locationNames = new Map(departmentLocations().map((location) => [location.code, location.name]));
  return departmentLinks().map((link) => ({
    ...link,
    location_name: link.page_type === GENERAL_KEY ? "General" : locationNames.get(link.location_code) || link.location_code
  }));
}

async function copyUrl(url, name) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(url);
    } else {
      fallbackCopy(url);
    }
    showCopyStatus(`Copied ${name}`);
  } catch {
    fallbackCopy(url);
    showCopyStatus(`Copied ${name}`);
  }
}

function fallbackCopy(text) {
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.left = "-9999px";
  document.body.append(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

function showCopyStatus(message) {
  let status = document.querySelector("#copyStatus");
  if (!status) {
    status = document.createElement("div");
    status.id = "copyStatus";
    status.className = "copy-status";
    status.setAttribute("role", "status");
    document.body.append(status);
  }
  status.textContent = message;
  status.classList.add("visible");
  window.clearTimeout(showCopyStatus.timeout);
  showCopyStatus.timeout = window.setTimeout(() => {
    status.classList.remove("visible");
  }, 1800);
}

function getRequestedLocation() {
  const params = new URLSearchParams(window.location.search);
  return (params.get("location") || GENERAL_KEY).trim().toLowerCase();
}

function updateLocationUrl(selected) {
  const url = new URL(window.location.href);
  if (selected === GENERAL_KEY) {
    url.searchParams.delete("location");
  } else {
    url.searchParams.set("location", selected);
  }
  const department = currentDepartment();
  if (department && state.departments.length > 1) {
    url.searchParams.set("department", department.slug);
  } else {
    url.searchParams.delete("department");
  }
  window.history.pushState({ location: selected }, "", `${url.pathname}${url.search}${url.hash}`);
}

function selectLocationFromUrl() {
  const requested = getRequestedLocation();
  const isKnownLocation = departmentLocations().some((location) => location.code === requested);
  elements.locationSelect.value = isKnownLocation ? requested : GENERAL_KEY;
}

function renderGeneral(query) {
  const allLinks = departmentLinks().filter((link) => link.page_type === GENERAL_KEY);
  const links = allLinks.filter((link) => matchesSearch(link, query));
  const groups = groupBy(links, "group_name");

  const department = currentDepartment();
  elements.viewMeta.textContent = department ? department.name : "General";
  elements.viewTitle.textContent = "Daily links";
  elements.resultCount.textContent = `${links.length} of ${allLinks.length} links`;
  elements.content.replaceChildren();

  if (!links.length) {
    const message = query
      ? "No general links match that search."
      : "No links have been added yet. Use the Admin center to build your catalog.";
    elements.content.append(createEmptyState(message));
    return;
  }

  Object.entries(groups).sort(compareByTitle).forEach(([group, items]) => {
    elements.content.append(createGroup(group, items));
  });
}

function renderLocation(location, query) {
  const allLinks = departmentLinks()
    .filter((link) => link.page_type === "location" && link.location_code === location.code)
    .map((link) => ({ ...link, location_name: location.name }));
  const visibleLinks = allLinks.filter((link) => matchesSearch(link, query));
  const standardLinks = visibleLinks.filter((link) => link.link_type !== "vhost");
  const vhostLinks = visibleLinks.filter((link) => link.link_type === "vhost");
  const vhostGroups = groupBy(vhostLinks, "group_name");

  elements.viewMeta.textContent = location.code.toUpperCase();
  elements.viewTitle.textContent = location.name;
  elements.resultCount.textContent = `${visibleLinks.length} of ${allLinks.length} links`;
  elements.content.replaceChildren();

  if (standardLinks.length) {
    elements.content.append(createGroup("Standard Services", standardLinks));
  }

  Object.entries(vhostGroups).sort(compareByTitle).forEach(([group, items]) => {
    elements.content.append(createGroup(group, items, `${items.length} vhosts`));
  });

  if (!visibleLinks.length) {
    elements.content.append(createEmptyState("No location links match that search."));
  }
}

function renderSearchResults(query) {
  const allLinks = allVisibleLinks();
  const links = allLinks.filter((link) => matchesSearch(link, query));
  const groups = groupBy(links, "location_name");

  elements.viewMeta.textContent = "All locations";
  elements.viewTitle.textContent = "Search results";
  elements.resultCount.textContent = `${links.length} of ${allLinks.length} links`;
  elements.content.replaceChildren();

  if (!links.length) {
    elements.content.append(createEmptyState("No links match that search."));
    return;
  }

  Object.entries(groups).sort(compareByTitle).forEach(([group, items]) => {
    elements.content.append(createGroup(group, items));
  });
}

function render() {
  const selected = elements.locationSelect.value;
  const query = elements.searchInput.value.trim().toLowerCase();

  if (query) {
    renderSearchResults(query);
    return;
  }

  if (selected === GENERAL_KEY) {
    renderGeneral(query);
    return;
  }

  const location = departmentLocations().find((item) => item.code === selected);
  if (location) renderLocation(location, query);
}

function populateLocations() {
  elements.locationSelect.replaceChildren();

  const generalOption = document.createElement("option");
  generalOption.value = GENERAL_KEY;
  generalOption.textContent = "General";
  elements.locationSelect.append(generalOption);

  [...departmentLocations()].sort(compareByName).forEach((location) => {
    const option = document.createElement("option");
    option.value = location.code;
    option.textContent = location.name;
    elements.locationSelect.append(option);
  });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  elements.themeLabel.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  updateBrandLogo();
}

function initializeTheme() {
  const savedTheme = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(savedTheme || (prefersDark ? "dark" : "light"));
}

function toggleTheme() {
  const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, nextTheme);
  applyTheme(nextTheme);
}

function showLoginError(message) {
  elements.portalLoginError.textContent = message || "";
  elements.portalLoginError.classList.toggle("hidden", !message);
}

function applyViewer(viewer = {}) {
  state.viewer = viewer;
  const signedIn = Boolean(viewer.authenticated);
  elements.viewerStrip.classList.toggle("hidden", !signedIn);
  if (signedIn) elements.viewerName.textContent = viewer.username || "";
  // The gate only appears when anonymous access is switched off and nobody is
  // signed in; otherwise the catalog renders as it always did.
  const gated = Boolean(viewer.requires_login) && !signedIn;
  elements.portalLogin.classList.toggle("hidden", !gated);
  elements.portalBody.classList.toggle("hidden", gated);
  return gated;
}

async function refreshEntraAvailability() {
  try {
    const session = await (await fetch("/api/session")).json();
    elements.portalEntra.classList.toggle("hidden", !session.entra_available);
  } catch {
    elements.portalEntra.classList.add("hidden");
  }
}

async function loadCatalog() {
  elements.content.replaceChildren(createEmptyState("Loading links..."));
  const response = await fetch("/api/catalog");
  if (!response.ok) throw new Error("Unable to load links.");
  const catalog = await response.json();
  state.locations = catalog.locations;
  state.links = catalog.links;
  state.departments = catalog.departments || [];
  applyProductNotice(catalog.product);
  applyBranding(catalog.branding);
  const gated = applyViewer(catalog.viewer);
  if (gated) {
    await refreshEntraAvailability();
    return;
  }
  populateDepartments();
  selectDepartmentFromUrl();
  populateLocations();
  selectLocationFromUrl();
  elements.searchInput.value = "";
  if (!departmentsAvailable()) {
    elements.content.replaceChildren(
      createEmptyState("No departments have been shared with you yet. Ask an administrator for access.")
    );
    return;
  }
  render();
}

initializeTheme();
loadCatalog().catch(() => {
  elements.content.replaceChildren(createEmptyState("The link catalog could not be loaded."));
});

elements.departmentSelect.addEventListener("change", () => {
  // Switching department changes which locations exist, so the location list is
  // rebuilt and reset rather than carried across.
  populateLocations();
  elements.locationSelect.value = GENERAL_KEY;
  updateLocationUrl(GENERAL_KEY);
  render();
});
elements.locationSelect.addEventListener("change", () => {
  updateLocationUrl(elements.locationSelect.value);
  render();
});
elements.portalLoginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  showLoginError("");
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: elements.portalUsername.value,
        password: elements.portalPassword.value
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Sign in failed.");
    elements.portalPassword.value = "";
    await loadCatalog();
  } catch (error) {
    showLoginError(error.message);
  }
});
elements.portalEntraButton.addEventListener("click", () => {
  window.location.assign("/api/auth/entra/start");
});
elements.signOutButton.addEventListener("click", async () => {
  await fetch("/api/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}"
  });
  window.location.assign("/");
});
elements.searchInput.addEventListener("input", render);
elements.themeToggle.addEventListener("click", toggleTheme);
window.addEventListener("popstate", () => {
  selectLocationFromUrl();
  render();
});
