const byId = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const homeTab = byId('tab-home');
const settingsTab = byId('tab-settings');
const newButton = byId('new-report');
const refreshButton = byId('refresh-reports');
const reportList = byId('report-list');
const draftDialog = byId('new-report-dialog');
const draftForm = byId('report-form');
const draftFields = byId('draft-fields');
const groupFilter = byId('group-filter');
const groupOptions = byId('group-options');
const rangeKind = byId('range-kind');
const rangeDays = byId('range-days');
const rangeStart = byId('range-start');
const rangeEnd = byId('range-end');
const previewButton = byId('preview-report');
const generateButton = byId('generate-report');
const detailDialog = byId('report-detail-dialog');
const settingsForm = byId('settings-form');
const settingsFields = byId('settings-fields');
const providerInput = byId('provider-id');
const promptInput = byId('prompt');
const saveButton = byId('save-settings');

const statusLabels = { running: '生成中', completed: '已完成', failed: '生成失败' };
const coverageExplanation = '覆盖情况仅表示本插件已知的采集时段，不保证包含群内的全部历史消息。';
const dayMs = 86_400_000;
const zone = { timeZone: 'Asia/Shanghai' };
const dateFormat = new Intl.DateTimeFormat('zh-CN', { ...zone, year: 'numeric', month: '2-digit', day: '2-digit' });
const timeFormat = new Intl.DateTimeFormat('zh-CN', { ...zone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const numberFormat = new Intl.NumberFormat('zh-CN');

let bootstrap = null;
let reports = null;
let refreshing = false;
let listVersion = 0;
let pollTimer;
let detail = null;
let saving = false;
let creating = false;
let draftVersion = 0;
let previewRequest = null;
let preview = null;
const selectedGroups = new Set();

function node(tag, className = '', text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = text;
  return element;
}

function showMessage(id, text = '') {
  const element = byId(id);
  element.textContent = text;
  element.hidden = !text;
}

function errorMessage(error) {
  return error?.message || '发生未知错误，请重试。';
}

function formatMoment(value, seconds = false) {
  const date = value instanceof Date ? value : new Date(value);
  const clock = timeFormat.format(date);
  return `${dateFormat.format(date)} ${seconds ? clock : clock.slice(0, 5)}`;
}

function timeRange(start, end, compact = false) {
  const startDate = new Date(start);
  const endDate = new Date(end);
  const midnight = (date) => date.getMilliseconds() === 0 && timeFormat.format(date) === '00:00:00';
  if (midnight(startDate) && midnight(endDate) && endDate > startDate) {
    const firstDay = dateFormat.format(startDate);
    const lastDay = dateFormat.format(new Date(endDate.getTime() - 1));
    return firstDay === lastDay ? firstDay : `${firstDay} 至 ${lastDay}`;
  }
  return `${formatMoment(startDate, !compact)} 至 ${formatMoment(endDate, !compact)}${compact ? '' : '（不含结束时刻）'}`;
}

function textCount(count) {
  return `${numberFormat.format(count)} 条文本`;
}

function statusBadge(status) {
  return Object.hasOwn(statusLabels, status) ? node('span', `badge ${status}`, statusLabels[status]) : node('span', 'badge', '未知状态');
}

function providerLabel(report) {
  return report.provider_label || report.provider_id || '未指定模型';
}

function groupLabels(groups) {
  return groups.map(({ key, label }) => label || key).join('、');
}

function selectTab(active, focus = false) {
  for (const tab of [homeTab, settingsTab]) {
    const selected = tab === active;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    byId(tab.getAttribute('aria-controls')).hidden = !selected;
  }
  if (focus) active.focus();
}

for (const tab of [homeTab, settingsTab]) {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', (event) => {
    const destination = {
      Home: homeTab,
      End: settingsTab,
      ArrowLeft: tab === homeTab ? settingsTab : homeTab,
      ArrowRight: tab === homeTab ? settingsTab : homeTab,
    }[event.key];
    if (!destination) return;
    event.preventDefault();
    selectTab(destination, true);
  });
}

function renderReports(nextReports) {
  const focusedId = document.activeElement?.dataset.reportId;
  let focusedRow;
  const fragment = document.createDocumentFragment();
  for (const report of nextReports) {
    const row = node('button', 'report-row');
    row.type = 'button';
    row.dataset.reportId = report.id;
    row.setAttribute('aria-haspopup', 'dialog');
    const groups = node('span', 'report-groups', groupLabels(report.groups));
    const metadata = node('span', 'report-meta', [timeRange(report.start, report.end, true), textCount(report.count), providerLabel(report)].join(' · '));
    const chevron = node('span', 'report-chevron');
    chevron.setAttribute('aria-hidden', 'true');
    row.append(groups, metadata, statusBadge(report.status), chevron);
    const item = node('li');
    item.append(row);
    fragment.append(item);
    if (row.dataset.reportId === focusedId) focusedRow = row;
  }
  reportList.replaceChildren(fragment);
  const counter = byId('report-count');
  counter.textContent = `${numberFormat.format(nextReports.length)} 份`;
  counter.hidden = false;
  byId('report-empty').hidden = nextReports.length !== 0;
  reportList.hidden = nextReports.length === 0;
  focusedRow?.focus({ preventScroll: true });
}

function updateReports(nextReports) {
  if (JSON.stringify(nextReports) !== JSON.stringify(reports)) {
    renderReports(nextReports);
    reports = nextReports;
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (reports?.some((report) => report.status === 'running') || detail?.report?.status === 'running') {
    pollTimer = setTimeout(() => refreshReports(), 2500);
  }
}

async function refreshReports() {
  if (refreshing) return;
  refreshing = true;
  clearTimeout(pollTimer);
  const version = listVersion;
  try {
    const result = await bridge.apiGet('reports');
    if (version === listVersion) updateReports(result.reports);
    showMessage('report-error');
  } catch (error) {
    showMessage('report-error', `无法刷新报告记录：${errorMessage(error)}`);
  } finally {
    byId('report-loading').hidden = true;
    refreshing = false;
    if (detailDialog.open && detail && (!detail.report || detail.report.status === 'running')) {
      void fetchDetail(detail);
    }
    schedulePoll();
  }
}

refreshButton.addEventListener('click', () => { void refreshReports(); });

function renderCoverage(coverage) {
  if (!coverage.length) return node('p', 'hint', '没有可显示的覆盖记录。');
  const list = node('ul', 'coverage-list');
  for (const group of coverage) {
    const item = node('li', 'coverage-item');
    const title = node('div', 'coverage-heading');
    title.append(node('strong', '', group.label || group.key), node('span', 'hint', textCount(group.count)));
    title.append(node('span', group.partial ? 'badge partial' : 'badge completed', group.partial ? '采集覆盖不完整' : '已知时段已覆盖'));
    item.append(title);
    if (group.started_at) item.append(node('p', 'coverage-note', `开始采集：${formatMoment(group.started_at, true)}`));
    if (group.first_at) item.append(node('p', 'coverage-note', `首条归档文本：${formatMoment(group.first_at, true)}`));
    if (group.gaps?.length) {
      const gaps = node('details', 'coverage-gaps');
      const intervals = node('ul');
      for (const gap of group.gaps) {
        intervals.append(node('li', '', `${formatMoment(gap.start, true)} 至 ${formatMoment(gap.end, true)}（不含结束时刻）`));
      }
      gaps.append(node('summary', '', `${group.gaps.length} 段未采集时段`), intervals);
      item.append(gaps);
    }
    list.append(item);
  }
  return list;
}

function renderDetail(report) {
  const content = byId('detail-content');
  const summary = node('dl', 'detail-meta');
  for (const [label, value] of [
    ['群组', report.groups.map(({ key, label }, i) => `G${i + 1}：${label || key}`).join('；')],
    ['报告模型', providerLabel(report)],
    ['时间范围（北京时间）', timeRange(report.start, report.end)],
    ['创建时间', formatMoment(report.created_at)],
    ['使用的消息', textCount(report.count)],
  ]) {
    const field = node('div');
    field.append(node('dt', '', label), node('dd', '', value));
    summary.append(field);
  }

  const message = node('section', 'detail-section');
  if (report.status === 'completed') {
    const body = node('pre', 'report-text', report.body || '报告正文为空。');
    body.id = 'report-body';
    message.append(node('h3', '', '报告正文'), body);
  } else if (report.status === 'failed') {
    const failure = node('p', 'notice', report.error || '服务未提供失败详情。');
    failure.id = 'detail-error';
    message.append(node('h3', '', '失败原因'), failure);
  } else {
    message.append(node('p', 'feedback', '报告正在生成，完成后会自动更新。'));
  }

  const coverage = node('section', 'detail-section');
  coverage.append(node('h3', '', '消息覆盖'), node('p', 'hint', coverageExplanation), renderCoverage(report.coverage));
  const prompt = node('details', 'detail-section prompt-snapshot');
  prompt.append(node('summary', '', '本次报告使用的提示词'), node('pre', 'report-text', report.prompt || '未提供提示词。'));
  const status = statusBadge(report.status);
  status.id = 'detail-status';
  status.setAttribute('role', 'status');
  content.className = 'detail-stack';
  content.replaceChildren(status, summary, message, coverage, prompt);
  content.hidden = false;
}

async function fetchDetail(view) {
  if (view.loading) return;
  view.loading = true;
  byId('retry-detail').disabled = true;
  try {
    const report = await bridge.apiGet(`reports/${encodeURIComponent(view.id)}`);
    if (detail !== view || !detailDialog.open) return;
    if (JSON.stringify(report) !== JSON.stringify(view.report)) renderDetail(report);
    view.report = report;
    if (!refreshing && report.status === 'running') schedulePoll();
    byId('detail-request-error').hidden = true;
  } catch (error) {
    if (detail === view && detailDialog.open) {
      byId('detail-request-message').textContent = `无法读取报告：${errorMessage(error)}`;
      byId('detail-request-error').hidden = false;
    }
  } finally {
    view.loading = false;
    if (detail === view && detailDialog.open) {
      byId('detail-loading').hidden = true;
      byId('retry-detail').disabled = false;
    }
  }
}

reportList.addEventListener('click', (event) => {
  const row = event.target.closest('.report-row');
  if (!row) return;
  detail = { id: row.dataset.reportId, report: null, loading: false, row };
  byId('detail-content').hidden = true;
  byId('detail-request-error').hidden = true;
  byId('detail-loading').hidden = false;
  detailDialog.showModal();
  detailDialog.querySelector('.dialog-body').scrollTop = 0;
  void fetchDetail(detail);
});

byId('close-report-detail').addEventListener('click', () => detailDialog.close());
byId('retry-detail').addEventListener('click', () => { if (detail) void fetchDetail(detail); });
detailDialog.addEventListener('close', () => {
  if (detail && !detail.row.isConnected) {
    const replacement = Array.from(reportList.querySelectorAll('.report-row')).find((row) => row.dataset.reportId === detail.id);
    (replacement || refreshButton).focus({ preventScroll: true });
  }
  detail = null;
});

function renderGroups() {
  const search = groupFilter.value.trim().toLocaleLowerCase('zh-CN');
  const fragment = document.createDocumentFragment();
  const groups = bootstrap.groups;
  let visibleCount = 0;
  for (const group of groups) {
    const name = group.label || group.key;
    if (search && !`${name} ${group.key}`.toLocaleLowerCase('zh-CN').includes(search)) continue;
    const label = node('label', 'group-option');
    label.title = group.key;
    const box = node('input');
    box.type = 'checkbox';
    box.name = 'group';
    box.value = group.key;
    box.checked = selectedGroups.has(group.key);
    label.append(box, node('span', '', name));
    fragment.append(label);
    visibleCount += 1;
  }
  groupOptions.replaceChildren(fragment);
  groupOptions.scrollTop = 0;
  showMessage('group-empty', visibleCount ? '' : groups.length ? '没有符合搜索条件的群组。' : '尚未配置采集群。请在 AstrBot 的插件配置中添加采集群并重载插件。');
}

function providerAvailable() {
  return bootstrap?.models.some((model) => model.id === bootstrap.settings.provider_id);
}

function updateDraftActions() {
  previewButton.disabled = !selectedGroups.size || Boolean(previewRequest) || creating;
  previewButton.textContent = previewRequest ? '预览中…' : '预览覆盖';
  generateButton.disabled = !preview?.count || !providerAvailable() || creating;
  generateButton.textContent = creating ? '提交中…' : '生成报告';
}

function invalidatePreview() {
  draftVersion += 1;
  preview = null;
  byId('preview-result').hidden = true;
  showMessage('draft-error');
  showMessage('draft-status');
  updateDraftActions();
}

function updateRangeFields() {
  const days = rangeKind.value === 'days';
  const dates = rangeKind.value === 'dates';
  byId('range-days-field').hidden = !days;
  byId('range-dates-fields').hidden = !dates;
  rangeDays.disabled = !days;
  rangeStart.disabled = !dates;
  rangeEnd.disabled = !dates;
}

newButton.addEventListener('click', () => {
  draftForm.reset();
  selectedGroups.clear();
  previewRequest = null;
  invalidatePreview();
  updateRangeFields();
  renderGroups();
  byId('group-count').textContent = '已选 0 个群组';
  showMessage('draft-model-note', providerAvailable() ? '' : '可以预览覆盖。生成报告前，请先到「配置」选择并保存报告模型。');
  draftDialog.showModal();
  draftDialog.querySelector('.dialog-body').scrollTop = 0;
});

byId('close-new-report').addEventListener('click', () => draftDialog.close());
draftDialog.addEventListener('cancel', (event) => { if (creating) event.preventDefault(); });
groupFilter.addEventListener('input', renderGroups);
groupOptions.addEventListener('change', (event) => {
  const box = event.target;
  if (box.name !== 'group') return;
  if (box.checked) selectedGroups.add(box.value);
  else selectedGroups.delete(box.value);
  byId('group-count').textContent = `已选 ${selectedGroups.size} 个群组`;
  invalidatePreview();
});
rangeKind.addEventListener('change', () => { updateRangeFields(); invalidatePreview(); });
for (const input of [rangeDays, rangeStart, rangeEnd]) input.addEventListener('input', invalidatePreview);

function draftPayload() {
  if (!selectedGroups.size || !draftForm.reportValidity()) return null;
  const kind = rangeKind.value;
  const range = { kind };
  if (kind === 'days') range.days = rangeDays.valueAsNumber;
  if (kind === 'dates') {
    range.start = rangeStart.value;
    range.end = rangeEnd.value;
    const length = (rangeEnd.valueAsNumber - rangeStart.valueAsNumber) / dayMs + 1;
    if (length < 1 || length > 366) {
      showMessage('draft-error', length < 1 ? '结束日期不能早于开始日期。' : '自定义时间范围不能超过 366 天。');
      rangeEnd.focus();
      return null;
    }
  }
  return { groups: [...selectedGroups], range };
}

previewButton.addEventListener('click', async () => {
  if (previewRequest || creating) return;
  const payload = draftPayload();
  if (!payload) return;
  invalidatePreview();
  const request = { version: draftVersion };
  previewRequest = request;
  updateDraftActions();
  showMessage('draft-status', '正在检查所选群组的消息覆盖…');
  try {
    const result = await bridge.apiPost('preview', payload);
    if (draftVersion !== request.version || !draftDialog.open) return;
    byId('preview-count').textContent = textCount(result.count);
    byId('preview-range').textContent = `${timeRange(result.start, result.end)} · 北京时间`;
    showMessage('preview-note', result.count === 0 ? '所选群组和时段没有归档文本，不能生成报告。' : '');
    byId('preview-coverage').replaceChildren(node('p', 'hint', coverageExplanation), renderCoverage(result.coverage));
    byId('preview-result').hidden = false;
    preview = { payload, count: result.count };
    showMessage('draft-status', `覆盖预览已更新：${textCount(result.count)}。`);
    byId('preview-result').scrollIntoView({ block: 'nearest' });
  } catch (error) {
    if (draftVersion === request.version && draftDialog.open) {
      showMessage('draft-error', `无法预览覆盖：${errorMessage(error)}`);
      showMessage('draft-status');
    }
  } finally {
    if (previewRequest === request) {
      previewRequest = null;
      updateDraftActions();
    }
  }
});

draftForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (creating || !preview?.count || !providerAvailable()) return;
  creating = true;
  draftFields.disabled = true;
  byId('close-new-report').disabled = true;
  updateDraftActions();
  showMessage('draft-error');
  showMessage('draft-status', '正在提交报告任务…');
  try {
    const report = await bridge.apiPost('reports', preview.payload);
    listVersion += 1;
    updateReports([report, ...(reports || []).filter(({ id }) => id !== report.id)]);
    byId('report-loading').hidden = true;
    showMessage('report-error');
    showMessage('home-notice', '报告已提交，请在记录列表查看进度。');
    draftDialog.close();
    schedulePoll();
  } catch (error) {
    showMessage('draft-error', `无法创建报告：${errorMessage(error)}`);
    showMessage('draft-status');
  } finally {
    creating = false;
    draftFields.disabled = false;
    byId('close-new-report').disabled = false;
    updateDraftActions();
  }
});

function renderSettings() {
  const { models, settings } = bootstrap;
  const available = providerAvailable();
  let placeholder = '请选择模型';
  if (!models.length) placeholder = '没有可用模型';
  else if (settings.provider_id && !available) placeholder = '已保存的模型不可用，请重新选择';
  const option = new Option(placeholder, '');
  option.disabled = true;
  providerInput.replaceChildren(option, ...models.map((model) => new Option(model.label || model.id, model.id)));
  providerInput.value = available ? settings.provider_id : '';
  providerInput.required = true;
  promptInput.value = settings.prompt;
  byId('model-help').textContent = models.length ? '从 AstrBot 中已经配置的聊天模型里选择。' : '请先在 AstrBot 中配置聊天模型，再重新打开此页面。';
  settingsFields.disabled = false;
  saveButton.disabled = !models.length || !providerInput.value;
}

settingsForm.addEventListener('input', () => {
  if (saving) return;
  saveButton.disabled = !providerInput.value;
  byId('settings-status').textContent = '';
  showMessage('settings-error');
});

settingsForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (saving || !providerInput.value) return;
  const payload = { provider_id: providerInput.value, prompt: promptInput.value };
  saving = true;
  settingsFields.disabled = true;
  saveButton.disabled = true;
  showMessage('settings-error');
  byId('settings-status').textContent = '正在保存…';
  try {
    bootstrap.settings = await bridge.apiPost('settings', payload);
    providerInput.value = bootstrap.settings.provider_id;
    promptInput.value = bootstrap.settings.prompt;
    byId('settings-status').textContent = '配置已保存。';
  } catch (error) {
    showMessage('settings-error', `无法保存配置：${errorMessage(error)}`);
    byId('settings-status').textContent = '';
  } finally {
    saving = false;
    settingsFields.disabled = false;
    saveButton.disabled = !providerInput.value;
  }
});

async function loadBootstrap() {
  const banner = byId('connection-error');
  const retry = byId('retry-bootstrap');
  retry.disabled = true;
  try {
    bootstrap = await bridge.apiGet('bootstrap');
    renderSettings();
    banner.hidden = true;
    newButton.disabled = false;
  } catch (error) {
    byId('connection-message').textContent = `无法加载群组和配置：${errorMessage(error)}`;
    banner.hidden = false;
  } finally {
    retry.disabled = false;
  }
}

byId('retry-bootstrap').addEventListener('click', () => { void loadBootstrap(); });

try {
  if (!bridge) throw new Error('请从 AstrBot WebUI 的插件可视化视图打开此页面。');
  await bridge.ready();
  refreshButton.disabled = false;
  void loadBootstrap();
  void refreshReports();
} catch (error) {
  byId('report-loading').hidden = true;
  byId('connection-message').textContent = `无法连接到 AstrBot：${errorMessage(error)}`;
  byId('retry-bootstrap').hidden = true;
  byId('connection-error').hidden = false;
}
