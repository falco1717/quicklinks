const GENERAL_KEY = "general";
const THEME_KEY = "quicklinks-theme";
const DEFAULT_LOGO = "/assets/quicklinks-logo.png";
const DEFAULT_DARK_LOGO = "/assets/quicklinks-logo-dark.png";

const state = {
  locations: [],
  links: [],
  branding: {},
  product: {
    notice: "QuickLinks · Created by Jordan Farmer"
  }
};

const elements = {
  brandLogo: document.querySelector("#brandLogo"),
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
  const locationNames = new Map(state.locations.map((location) => [location.code, location.name]));
  return state.links.map((link) => ({
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
  window.history.pushState({ location: selected }, "", `${url.pathname}${url.search}${url.hash}`);
}

function selectLocationFromUrl() {
  const requested = getRequestedLocation();
  const isKnownLocation = state.locations.some((location) => location.code === requested);
  elements.locationSelect.value = isKnownLocation ? requested : GENERAL_KEY;
}

function renderGeneral(query) {
  const allLinks = state.links.filter((link) => link.page_type === GENERAL_KEY);
  const links = allLinks.filter((link) => matchesSearch(link, query));
  const groups = groupBy(links, "group_name");

  elements.viewMeta.textContent = "General";
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
  const allLinks = state.links
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

  const location = state.locations.find((item) => item.code === selected);
  if (location) renderLocation(location, query);
}

function populateLocations() {
  elements.locationSelect.replaceChildren();

  const generalOption = document.createElement("option");
  generalOption.value = GENERAL_KEY;
  generalOption.textContent = "General";
  elements.locationSelect.append(generalOption);

  [...state.locations].sort(compareByName).forEach((location) => {
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

async function loadCatalog() {
  elements.content.replaceChildren(createEmptyState("Loading links..."));
  const response = await fetch("/api/catalog");
  if (!response.ok) throw new Error("Unable to load links.");
  const catalog = await response.json();
  state.locations = catalog.locations;
  state.links = catalog.links;
  applyProductNotice(catalog.product);
  applyBranding(catalog.branding);
  populateLocations();
  selectLocationFromUrl();
  elements.searchInput.value = "";
  render();
}

initializeTheme();
loadCatalog().catch(() => {
  elements.content.replaceChildren(createEmptyState("The link catalog could not be loaded."));
});

elements.locationSelect.addEventListener("change", () => {
  updateLocationUrl(elements.locationSelect.value);
  render();
});
elements.searchInput.addEventListener("input", render);
elements.themeToggle.addEventListener("click", toggleTheme);
window.addEventListener("popstate", () => {
  selectLocationFromUrl();
  render();
});
