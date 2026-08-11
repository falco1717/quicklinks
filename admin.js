const state = {
  authenticated: false,
  activeTab: "links",
  locations: [],
  links: [],
  branding: {},
  product: {
    notice: "QuickLinks · Created by Jordan Farmer"
  },
  auth: { users: [], ad: {} },
  selectedLinkId: null,
  selectedLocationId: null
};

const THEME_KEY = "quicklinks-theme";
const DEFAULT_LOGO = "/assets/quicklinks-logo.png";
const DEFAULT_DARK_LOGO = "/assets/quicklinks-logo-dark.png";

const el = {
  setupPanel: document.querySelector("#setupPanel"),
  setupForm: document.querySelector("#setupForm"),
  setupUsername: document.querySelector("#setupUsername"),
  setupPassword: document.querySelector("#setupPassword"),
  setupPasswordConfirm: document.querySelector("#setupPasswordConfirm"),
  adminBrandLogo: document.querySelector("#adminBrandLogo"),
  adminCompanyName: document.querySelector("#adminCompanyName"),
  adminBrandTitle: document.querySelector("#adminBrandTitle"),
  loginPanel: document.querySelector("#loginPanel"),
  adminPanel: document.querySelector("#adminPanel"),
  loginForm: document.querySelector("#loginForm"),
  usernameInput: document.querySelector("#usernameInput"),
  passwordInput: document.querySelector("#passwordInput"),
  themeToggle: document.querySelector("#themeToggle"),
  themeLabel: document.querySelector("#themeLabel"),
  logoutButton: document.querySelector("#logoutButton"),
  adminStatus: document.querySelector("#adminStatus"),
  adminFilter: document.querySelector("#adminFilter"),
  adminList: document.querySelector("#adminList"),
  tabButtons: [...document.querySelectorAll(".tab-button")],
  linksEditor: document.querySelector("#linksEditor"),
  locationsEditor: document.querySelector("#locationsEditor"),
  brandingEditor: document.querySelector("#brandingEditor"),
  dataEditor: document.querySelector("#dataEditor"),
  authenticationEditor: document.querySelector("#authenticationEditor"),
  adminLayout: document.querySelector("#adminLayout"),
  adminSidebar: document.querySelector("#adminSidebar"),
  linkFormTitle: document.querySelector("#linkFormTitle"),
  linkForm: document.querySelector("#linkForm"),
  linkId: document.querySelector("#linkId"),
  pageType: document.querySelector("#pageType"),
  linkLocation: document.querySelector("#linkLocation"),
  linkType: document.querySelector("#linkType"),
  linkSort: document.querySelector("#linkSort"),
  linkName: document.querySelector("#linkName"),
  linkUrl: document.querySelector("#linkUrl"),
  linkDescription: document.querySelector("#linkDescription"),
  linkGroup: document.querySelector("#linkGroup"),
  linkCluster: document.querySelector("#linkCluster"),
  linkEnabled: document.querySelector("#linkEnabled"),
  newLinkButton: document.querySelector("#newLinkButton"),
  deleteLinkButton: document.querySelector("#deleteLinkButton"),
  locationFormTitle: document.querySelector("#locationFormTitle"),
  locationForm: document.querySelector("#locationForm"),
  locationId: document.querySelector("#locationId"),
  locationName: document.querySelector("#locationName"),
  locationCode: document.querySelector("#locationCode"),
  locationSort: document.querySelector("#locationSort"),
  locationEnabled: document.querySelector("#locationEnabled"),
  newLocationButton: document.querySelector("#newLocationButton"),
  deleteLocationButton: document.querySelector("#deleteLocationButton"),
  brandingForm: document.querySelector("#brandingForm"),
  brandingPreview: document.querySelector("#brandingPreview"),
  brandingCompanyName: document.querySelector("#brandingCompanyName"),
  brandingDepartmentTitle: document.querySelector("#brandingDepartmentTitle"),
  brandingAdminTitle: document.querySelector("#brandingAdminTitle"),
  brandingLogo: document.querySelector("#brandingLogo"),
  removeBrandingLogo: document.querySelector("#removeBrandingLogo"),
  importFile: document.querySelector("#importFile"),
  importMode: document.querySelector("#importMode"),
  importButton: document.querySelector("#importButton"),
  importResult: document.querySelector("#importResult"),
  localUserList: document.querySelector("#localUserList"),
  localUserForm: document.querySelector("#localUserForm"),
  localUserId: document.querySelector("#localUserId"),
  localUsername: document.querySelector("#localUsername"),
  localPassword: document.querySelector("#localPassword"),
  localUserEnabled: document.querySelector("#localUserEnabled"),
  newLocalUserButton: document.querySelector("#newLocalUserButton"),
  deleteLocalUserButton: document.querySelector("#deleteLocalUserButton"),
  adForm: document.querySelector("#adForm"),
  adEnabled: document.querySelector("#adEnabled"),
  adServer: document.querySelector("#adServer"),
  adSsl: document.querySelector("#adSsl"),
  adDomain: document.querySelector("#adDomain"),
  adAdminUsers: document.querySelector("#adAdminUsers"),
  adAdminGroups: document.querySelector("#adAdminGroups"),
  entraLogin: document.querySelector("#entraLogin"),
  entraLoginButton: document.querySelector("#entraLoginButton"),
  loginError: document.querySelector("#loginError"),
  entraForm: document.querySelector("#entraForm"),
  entraEnabled: document.querySelector("#entraEnabled"),
  entraTenantId: document.querySelector("#entraTenantId"),
  entraClientId: document.querySelector("#entraClientId"),
  entraClientSecret: document.querySelector("#entraClientSecret"),
  entraSecretState: document.querySelector("#entraSecretState"),
  entraRedirectUri: document.querySelector("#entraRedirectUri"),
  entraAdminUsers: document.querySelector("#entraAdminUsers"),
  entraAdminGroups: document.querySelector("#entraAdminGroups"),
  entraAdminRoles: document.querySelector("#entraAdminRoles"),
  productNotice: document.querySelector("#productNotice")
};

const ENTRA_ERRORS = {
  config: "Microsoft Entra ID sign-in is not configured yet. An administrator can set it up under Authentication.",
  denied: "Microsoft did not complete the sign-in. You can try again or use a local account.",
  state: "That sign-in could not be matched to this browser. Start again from this page.",
  token: "The Microsoft sign-in could not be verified. Check the server log for details.",
  forbidden: "That Microsoft account is not an allowed QuickLinks administrator."
};

function applyProductNotice(product = {}) {
  state.product = { ...state.product, ...product };
  el.productNotice.textContent = state.product.notice;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  el.themeLabel.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  updateBrandLogo();
}

function isDefaultLogo(logoUrl = "") {
  return !logoUrl || logoUrl === DEFAULT_LOGO || logoUrl.endsWith(DEFAULT_LOGO);
}

function updateBrandLogo() {
  const logoUrl = state.branding?.logo_url || DEFAULT_LOGO;
  const isDark = document.documentElement.dataset.theme === "dark";
  el.adminBrandLogo.src = isDark && isDefaultLogo(logoUrl) ? DEFAULT_DARK_LOGO : logoUrl;
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

initializeTheme();
el.themeToggle?.addEventListener("click", toggleTheme);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "Request failed.");
  return payload;
}

function setStatus(message) {
  el.adminStatus.textContent = message;
}

function showApp(authenticated, setupRequired = false) {
  state.authenticated = authenticated;
  el.setupPanel.classList.toggle("hidden", !setupRequired || authenticated);
  el.loginPanel.classList.toggle("hidden", authenticated || setupRequired);
  el.adminPanel.classList.toggle("hidden", !authenticated);
  el.logoutButton.classList.toggle("hidden", !authenticated);
}

function locationName(code) {
  return state.locations.find((location) => location.code === code)?.name || "General";
}

function populateLocationSelect() {
  el.linkLocation.replaceChildren();
  state.locations.forEach((location) => {
    const option = document.createElement("option");
    option.value = location.code;
    option.textContent = `${location.name} (${location.code})`;
    el.linkLocation.append(option);
  });
}

function renderList() {
  const query = el.adminFilter.value.trim().toLowerCase();
  el.adminList.replaceChildren();

  if (state.activeTab === "links") {
    const links = state.links.filter((link) => {
      const haystack = [link.name, link.url, link.description, link.group_name, link.cluster, link.location_code, link.page_type]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
    links.forEach((link) => el.adminList.append(createListButton(link)));
    setStatus(`${links.length} of ${state.links.length} links`);
    return;
  }

  if (state.activeTab !== "locations") {
    const labels = {
      branding: "Site logo and titles",
      data: "Bulk catalog tools",
      authentication: "Login and access settings"
    };
    setStatus(labels[state.activeTab] || "");
    return;
  }

  const locations = state.locations.filter((location) => {
    const haystack = `${location.name} ${location.code}`.toLowerCase();
    return haystack.includes(query);
  });
  locations.forEach((location) => el.adminList.append(createLocationButton(location)));
  setStatus(`${locations.length} of ${state.locations.length} locations`);
}

function showLoginError(message) {
  el.loginError.textContent = message || "";
  el.loginError.classList.toggle("hidden", !message);
}

function reportEntraRedirect() {
  const params = new URLSearchParams(window.location.search);
  const reason = params.get("entra_error");
  if (!reason) return;
  // Only ever render a message this page owns, never the raw query value.
  showLoginError(ENTRA_ERRORS[reason] || ENTRA_ERRORS.token);
  params.delete("entra_error");
  const query = params.toString();
  window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
}

function renderAuthentication() {
  const { users = [], ad = {}, entra = {} } = state.auth;
  el.localUserList.replaceChildren();
  users.forEach((user) => {
    const button = document.createElement("button");
    button.className = "list-item";
    button.type = "button";
    button.innerHTML = '<span class="list-title"></span><span class="list-meta"></span>';
    button.querySelector(".list-title").textContent = user.username;
    button.querySelector(".list-meta").textContent = user.enabled ? "Enabled local administrator" : "Disabled";
    button.addEventListener("click", () => selectLocalUser(user.id));
    el.localUserList.append(button);
  });
  el.adEnabled.checked = Boolean(ad.enabled);
  el.adServer.value = ad.server || "";
  el.adSsl.checked = ad.ssl !== false;
  el.adDomain.value = ad.domain || "";
  el.adAdminUsers.value = ad.admin_users || "";
  el.adAdminGroups.value = ad.admin_groups || "";

  el.entraEnabled.checked = Boolean(entra.enabled);
  el.entraTenantId.value = entra.tenant_id || "";
  el.entraClientId.value = entra.client_id || "";
  el.entraClientSecret.value = "";
  el.entraRedirectUri.value = entra.redirect_uri || defaultEntraRedirectUri();
  el.entraAdminUsers.value = entra.admin_users || "";
  el.entraAdminGroups.value = entra.admin_groups || "";
  el.entraAdminRoles.value = entra.admin_roles || "";
  el.entraSecretState.textContent = entra.client_secret_set
    ? "A client secret is stored. Leave the field blank to keep it, or enter a new one to replace it."
    : "No client secret is stored yet. One is required before Entra login can be enabled.";
}

function defaultEntraRedirectUri() {
  return `${window.location.origin}/api/auth/entra/callback`;
}

function resetLocalUser() {
  el.localUserId.value = "";
  el.localUsername.value = "";
  el.localPassword.value = "";
  el.localUserEnabled.checked = true;
}

function selectLocalUser(id) {
  const user = state.auth.users.find((item) => item.id === id);
  if (!user) return;
  el.localUserId.value = user.id;
  el.localUsername.value = user.username;
  el.localPassword.value = "";
  el.localUserEnabled.checked = Boolean(user.enabled);
}

function applyBranding() {
  const branding = state.branding || {};
  const companyName = branding.company_name || "QuickLinks";
  const adminTitle = branding.admin_title || "Admin Center";
  const logoUrl = branding.logo_url || DEFAULT_LOGO;
  el.adminBrandLogo.alt = companyName;
  el.adminCompanyName.textContent = companyName;
  el.adminBrandTitle.textContent = adminTitle;
  el.brandingPreview.src = logoUrl;
  el.brandingCompanyName.value = companyName;
  el.brandingDepartmentTitle.value = branding.department_title || "Link Portal";
  el.brandingAdminTitle.value = adminTitle;
  el.brandingLogo.value = "";
  el.removeBrandingLogo.checked = false;
  document.title = `${companyName} ${adminTitle} Admin`;
  updateBrandLogo();
}

function createListButton(link) {
  const button = document.createElement("button");
  button.className = "list-item";
  if (link.id === state.selectedLinkId) button.classList.add("active");
  button.type = "button";
  button.innerHTML = `
    <span class="list-title"></span>
    <span class="list-meta"></span>
    <span class="list-meta"></span>
  `;
  button.querySelector(".list-title").textContent = link.name;
  button.querySelectorAll(".list-meta")[0].textContent =
    link.page_type === "general" ? "General" : `${locationName(link.location_code)} - ${link.link_type}`;
  button.querySelectorAll(".list-meta")[1].textContent = `${link.group_name}${link.enabled ? "" : " - hidden"}`;
  button.addEventListener("click", () => selectLink(link.id));
  return button;
}

function createLocationButton(location) {
  const button = document.createElement("button");
  button.className = "list-item";
  if (location.id === state.selectedLocationId) button.classList.add("active");
  button.type = "button";
  button.innerHTML = `
    <span class="list-title"></span>
    <span class="list-meta"></span>
  `;
  button.querySelector(".list-title").textContent = location.name;
  button.querySelector(".list-meta").textContent = `${location.code}${location.enabled ? "" : " - hidden"}`;
  button.addEventListener("click", () => selectLocation(location.id));
  return button;
}

function resetLinkForm() {
  state.selectedLinkId = null;
  el.linkFormTitle.textContent = "Add link";
  el.linkId.value = "";
  el.pageType.value = "general";
  el.linkType.value = "general";
  el.linkSort.value = nextLinkSort();
  el.linkName.value = "";
  el.linkUrl.value = "";
  el.linkDescription.value = "";
  el.linkGroup.value = "Operations";
  el.linkCluster.value = "";
  el.linkEnabled.checked = true;
  updateLinkLocationState();
  renderList();
}

function selectLink(id) {
  const link = state.links.find((item) => item.id === id);
  if (!link) return;
  state.selectedLinkId = id;
  el.linkFormTitle.textContent = "Edit link";
  el.linkId.value = link.id;
  el.pageType.value = link.page_type;
  el.linkLocation.value = link.location_code || state.locations[0]?.code || "";
  el.linkType.value = link.link_type;
  el.linkSort.value = link.sort_order;
  el.linkName.value = link.name;
  el.linkUrl.value = link.url;
  el.linkDescription.value = link.description;
  el.linkGroup.value = link.group_name;
  el.linkCluster.value = link.cluster;
  el.linkEnabled.checked = Boolean(link.enabled);
  updateLinkLocationState();
  renderList();
}

function resetLocationForm() {
  state.selectedLocationId = null;
  el.locationFormTitle.textContent = "Add location";
  el.locationId.value = "";
  el.locationName.value = "";
  el.locationCode.value = "";
  el.locationSort.value = nextLocationSort();
  el.locationEnabled.checked = true;
  renderList();
}

function selectLocation(id) {
  const location = state.locations.find((item) => item.id === id);
  if (!location) return;
  state.selectedLocationId = id;
  el.locationFormTitle.textContent = "Edit location";
  el.locationId.value = location.id;
  el.locationName.value = location.name;
  el.locationCode.value = location.code;
  el.locationSort.value = location.sort_order;
  el.locationEnabled.checked = Boolean(location.enabled);
  renderList();
}

function nextLinkSort() {
  return Math.max(0, ...state.links.map((link) => Number(link.sort_order) || 0)) + 10;
}

function nextLocationSort() {
  return Math.max(0, ...state.locations.map((location) => Number(location.sort_order) || 0)) + 10;
}

function updateLinkLocationState() {
  const isLocation = el.pageType.value === "location";
  el.linkLocation.disabled = !isLocation;
  if (!isLocation) {
    el.linkType.value = "general";
    el.linkGroup.placeholder = "Operations";
  }
}

function switchTab(tab) {
  state.activeTab = tab;
  const isTool = ["branding", "data", "authentication"].includes(tab);
  el.tabButtons.forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  el.linksEditor.classList.toggle("hidden", tab !== "links");
  el.locationsEditor.classList.toggle("hidden", tab !== "locations");
  el.brandingEditor.classList.toggle("hidden", tab !== "branding");
  el.dataEditor.classList.toggle("hidden", tab !== "data");
  el.authenticationEditor.classList.toggle("hidden", tab !== "authentication");
  el.adminSidebar.classList.toggle("hidden", isTool);
  el.adminLayout.classList.toggle("tool-layout", isTool);
  renderList();
}

async function refreshAdmin() {
  const [payload, auth] = await Promise.all([api("/api/admin"), api("/api/auth-config")]);
  state.locations = payload.locations;
  state.links = payload.links;
  state.branding = payload.branding;
  applyProductNotice(payload.product);
  state.auth = auth;
  populateLocationSelect();
  applyBranding();
  renderAuthentication();
  renderList();
}

async function checkSession() {
  const payload = await api("/api/session");
  showApp(payload.authenticated, payload.setup_required);
  el.entraLogin.classList.toggle(
    "hidden",
    !payload.entra_available || payload.authenticated || payload.setup_required
  );
  if (payload.authenticated) {
    await refreshAdmin();
    resetLinkForm();
    resetLocationForm();
  }
}

el.setupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (el.setupPassword.value !== el.setupPasswordConfirm.value) {
    alert("Passwords do not match.");
    return;
  }
  try {
    await api("/api/setup", {
      method: "POST",
      body: JSON.stringify({ username: el.setupUsername.value, password: el.setupPassword.value })
    });
    el.setupForm.reset();
    showApp(true, false);
    await refreshAdmin();
    resetLinkForm();
    resetLocationForm();
  } catch (error) {
    alert(error.message);
  }
});

el.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  showLoginError("");
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: el.usernameInput.value, password: el.passwordInput.value })
    });
    el.passwordInput.value = "";
    showApp(true);
    el.entraLogin.classList.add("hidden");
    await refreshAdmin();
    resetLinkForm();
  } catch (error) {
    showLoginError(error.message);
  }
});

el.entraLoginButton.addEventListener("click", () => {
  window.location.assign("/api/auth/entra/start");
});

el.entraForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    state.auth = await api("/api/entra-config", {
      method: "POST",
      body: JSON.stringify({
        enabled: el.entraEnabled.checked,
        tenant_id: el.entraTenantId.value,
        client_id: el.entraClientId.value,
        client_secret: el.entraClientSecret.value,
        redirect_uri: el.entraRedirectUri.value,
        admin_users: el.entraAdminUsers.value,
        admin_groups: el.entraAdminGroups.value,
        admin_roles: el.entraAdminRoles.value
      })
    });
    renderAuthentication();
    setStatus("Microsoft Entra ID settings saved.");
  } catch (error) {
    alert(error.message);
  }
});

el.logoutButton.addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", body: "{}" });
  showApp(false);
});

el.tabButtons.forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
});

el.adminFilter.addEventListener("input", renderList);
el.newLinkButton.addEventListener("click", resetLinkForm);
el.newLocationButton.addEventListener("click", resetLocationForm);
el.pageType.addEventListener("change", updateLinkLocationState);
el.newLocalUserButton.addEventListener("click", resetLocalUser);
el.brandingLogo.addEventListener("change", () => {
  const file = el.brandingLogo.files[0];
  if (!file) return;
  el.brandingPreview.src = URL.createObjectURL(file);
  el.removeBrandingLogo.checked = false;
});
el.removeBrandingLogo.addEventListener("change", () => {
  if (el.removeBrandingLogo.checked) {
    el.brandingLogo.value = "";
    el.brandingPreview.src = DEFAULT_LOGO;
  } else {
    el.brandingPreview.src = state.branding.logo_url || DEFAULT_LOGO;
  }
});

el.brandingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const file = el.brandingLogo.files[0];
    const logoData = file ? await fileToDataUrl(file) : null;
    const updated = await api("/api/branding", {
      method: "POST",
      body: JSON.stringify({
        company_name: el.brandingCompanyName.value,
        department_title: el.brandingDepartmentTitle.value,
        admin_title: el.brandingAdminTitle.value,
        logo_data: logoData,
        remove_logo: el.removeBrandingLogo.checked
      })
    });
    state.branding = updated.branding;
    applyBranding();
    setStatus("Branding saved.");
  } catch (error) {
    alert(error.message);
  }
});

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Unable to read that logo file."));
    reader.readAsDataURL(file);
  });
}

el.importButton.addEventListener("click", async () => {
  const file = el.importFile.files[0];
  if (!file) {
    alert("Choose a CSV file first.");
    return;
  }
  if (el.importMode.value === "replace" &&
      !confirm("Replace every existing link and location with this CSV? This cannot be undone from the admin page.")) {
    return;
  }
  try {
    const csv = await file.text();
    const updated = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ csv, mode: el.importMode.value })
    });
    state.locations = updated.locations;
    state.links = updated.links;
    populateLocationSelect();
    renderList();
    el.importResult.textContent =
      `Imported ${updated.imported.locations} locations and ${updated.imported.links} links (${updated.imported.mode}).`;
    el.importFile.value = "";
  } catch (error) {
    alert(error.message);
  }
});

el.localUserForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const updated = await api("/api/admin-users", {
      method: "POST",
      body: JSON.stringify({
        id: el.localUserId.value || null,
        username: el.localUsername.value,
        password: el.localPassword.value,
        enabled: el.localUserEnabled.checked
      })
    });
    state.auth = updated;
    renderAuthentication();
    resetLocalUser();
    setStatus("Local administrator saved.");
  } catch (error) {
    alert(error.message);
  }
});

el.deleteLocalUserButton.addEventListener("click", async () => {
  if (!el.localUserId.value || !confirm("Delete this local administrator?")) return;
  try {
    state.auth = await api(`/api/admin-users/${el.localUserId.value}`, { method: "DELETE" });
    renderAuthentication();
    resetLocalUser();
    setStatus("Local administrator deleted.");
  } catch (error) {
    alert(error.message);
  }
});

el.adForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    state.auth = await api("/api/auth-config", {
      method: "POST",
      body: JSON.stringify({
        enabled: el.adEnabled.checked,
        server: el.adServer.value,
        port: el.adSsl.checked ? 636 : 389,
        ssl: el.adSsl.checked,
        domain: el.adDomain.value,
        admin_users: el.adAdminUsers.value,
        admin_groups: el.adAdminGroups.value
      })
    });
    renderAuthentication();
    setStatus("Active Directory settings saved.");
  } catch (error) {
    alert(error.message);
  }
});

el.linkForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = {
      id: el.linkId.value || null,
      page_type: el.pageType.value,
      location_code: el.pageType.value === "location" ? el.linkLocation.value : null,
      link_type: el.linkType.value,
      sort_order: Number(el.linkSort.value) || 0,
      name: el.linkName.value,
      url: el.linkUrl.value,
      description: el.linkDescription.value,
      group_name: el.linkGroup.value,
      cluster: el.linkCluster.value,
      enabled: el.linkEnabled.checked
    };
    const updated = await api("/api/links", { method: "POST", body: JSON.stringify(payload) });
    state.locations = updated.locations;
    state.links = updated.links;
    populateLocationSelect();
    renderList();
    setStatus("Link saved.");
  } catch (error) {
    alert(error.message);
  }
});

el.deleteLinkButton.addEventListener("click", async () => {
  if (!state.selectedLinkId) return;
  if (!confirm("Delete this link?")) return;
  const updated = await api(`/api/links/${state.selectedLinkId}`, { method: "DELETE" });
  state.locations = updated.locations;
  state.links = updated.links;
  resetLinkForm();
});

el.locationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = {
      id: el.locationId.value || null,
      name: el.locationName.value,
      code: el.locationCode.value,
      sort_order: Number(el.locationSort.value) || 0,
      enabled: el.locationEnabled.checked
    };
    const updated = await api("/api/locations", { method: "POST", body: JSON.stringify(payload) });
    state.locations = updated.locations;
    state.links = updated.links;
    populateLocationSelect();
    renderList();
    setStatus("Location saved.");
  } catch (error) {
    alert(error.message);
  }
});

el.deleteLocationButton.addEventListener("click", async () => {
  if (!state.selectedLocationId) return;
  if (!confirm("Delete this location and all of its links?")) return;
  const updated = await api(`/api/locations/${state.selectedLocationId}`, { method: "DELETE" });
  state.locations = updated.locations;
  state.links = updated.links;
  populateLocationSelect();
  resetLocationForm();
});

reportEntraRedirect();
checkSession().catch(() => showApp(false, false));
