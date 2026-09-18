// src/ui/app.js
const API_BASE = "http://localhost:8000/api/v1";

let selectedProfile = null;


// ============================================================================
// Профили
// ============================================================================

async function loadProfiles() {
    const grid = document.getElementById("profilesGrid");
    try {
        const response = await fetch(`${API_BASE}/profiles`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        grid.innerHTML = "";
        data.profiles.forEach(profile => {
            const card = document.createElement("div");
            card.className = "profile-card";
            card.dataset.profile = profile.name;
            const maxWeight = (profile.parameters.max_asset_weight * 100).toFixed(0);

            const cooldown = profile.parameters.cooldown_hours || 24;
            const noTrade = ((profile.parameters.no_trade_threshold || 0.05) * 100).toFixed(0);
            const maxTurnover = ((profile.parameters.max_turnover || 0.20) * 100).toFixed(0);

            const deleteButton = profile.is_custom
                ? `<button onclick="event.stopPropagation(); deleteCustomProfile('${profile.name}', '${profile.display_name}')"
                    style="position: absolute; top: 8px; right: 8px; width: 32px; height: 32px;
                        background: #fee2e2; border: 1px solid #fca5a5; color: #dc2626;
                        font-size: 20px; font-weight: 900; cursor: pointer; line-height: 1;
                        display: flex; align-items: center; justify-content: center;
                        border-radius: 6px; padding: 0;"
                    title="Удалить профиль">✕</button>`
                : '';

            card.innerHTML = `
                ${deleteButton}
                <div class="title">${profile.display_name}</div>
                <div class="desc">${profile.description}</div>
                <div class="params">Модель: ${profile.parameters.model_name} | Макс. доля: ${maxWeight}%</div>
                <div class="params" style="color: #64748b;">
                    ⏱ Cooldown: ${cooldown} ч · 🚫 No-trade: ${noTrade}% · 🔄 Turnover: ${maxTurnover}%
                </div>
            `;
            card.addEventListener("click", () => selectProfile(profile.name));
            grid.appendChild(card);
        });

        const defaultCard = grid.querySelector('[data-profile="balanced"]');
        if (defaultCard) defaultCard.click();
    } catch (error) {
        console.error("[UI] Ошибка загрузки профилей:", error);
        grid.innerHTML = '<div class="profile-card">Ошибка загрузки профилей</div>';
    }
}


function selectProfile(profileName) {
    selectedProfile = profileName;
    document.querySelectorAll(".profile-card").forEach(card => {
        card.classList.toggle("selected", card.dataset.profile === profileName);
    });
}


function toggleManual() {
    const section = document.getElementById("manualSection");
    const saveBox = document.getElementById("saveProfileBox");
    section.classList.toggle("open");

    if (section.classList.contains("open")) {
        selectedProfile = null;
        document.querySelectorAll(".profile-card").forEach(card => card.classList.remove("selected"));
        if (saveBox) saveBox.style.display = "block";
    } else {
        if (saveBox) saveBox.style.display = "none";
    }
}


async function loadModels() {
    const select = document.getElementById("modelName");
    try {
        const response = await fetch(`${API_BASE}/models`);
        const data = await response.json();
        select.innerHTML = "";
        data.models.forEach(model => {
            const option = document.createElement("option");
            option.value = model.name;
            option.textContent = model.description;
            select.appendChild(option);
        });
    } catch (error) {
        select.innerHTML = '<option value="xgboost">XGBoost (fallback)</option>';
    }
}


// ============================================================================
// ПОРТФЕЛЬ
// ============================================================================

function togglePortfolio() {
    const section = document.getElementById("portfolioSection");
    const arrow = document.getElementById("portfolioArrow");
    if (section.style.display === "none") {
        section.style.display = "block";
        arrow.textContent = "▾";
        loadPortfolio();
    } else {
        section.style.display = "none";
        arrow.textContent = "▸";
    }
}


async function loadPortfolio() {
    const view = document.getElementById("portfolioView");
    view.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 20px;">Загрузка...</div>';

    try {
        const response = await fetch(`${API_BASE}/portfolio`);
        const data = await response.json();

        const positions = data.positions || {};
        const avgPrices = data.avg_prices || {};
        const tickers = Object.keys(positions);

        if (tickers.length === 0) {
            view.innerHTML = `
                <div style="text-align: center; color: #94a3b8; padding: 20px;">
                    У вас пока нет позиций. Добавьте — система учтёт их при ребалансировке.
                </div>
                <div style="text-align: center; margin-top: 12px;">
                    <button class="btn-primary" onclick="startPortfolioEdit()">Добавить позиции</button>
                </div>
            `;
            return;
        }

        let totalValue = 0;
        tickers.forEach(t => {
            const price = avgPrices[t] || 0;
            totalValue += positions[t] * price;
        });

        let html = '<table class="portfolio-table"><thead><tr>';
        html += '<th>Тикер</th><th>Кол-во</th><th>Средняя цена</th><th>Стоимость</th>';
        html += '</tr></thead><tbody>';

        tickers.forEach(t => {
            const qty = positions[t];
            const price = avgPrices[t] || 0;
            const value = qty * price;
            html += `<tr>
                <td><strong>${t}</strong></td>
                <td>${qty}</td>
                <td>${price.toFixed(2)} ₽</td>
                <td>${value.toFixed(2)} ₽</td>
            </tr>`;
        });

        html += '</tbody></table>';
        html += `<div style="margin-top: 12px; font-size: 14px; color: #475569;">
            Общая стоимость: <strong>${totalValue.toFixed(2)} ₽</strong>
        </div>`;
        html += `<div style="margin-top: 12px; display: flex; gap: 8px;">
            <button class="btn-secondary" onclick="startPortfolioEdit()">✎ Редактировать</button>
            <button class="btn-secondary" onclick="loadPortfolio()">🔄 Обновить</button>
        </div>`;

        view.innerHTML = html;
    } catch (error) {
        view.innerHTML = `<div style="text-align: center; color: #dc2626; padding: 20px;">Ошибка: ${error.message}</div>`;
    }
}


async function startPortfolioEdit() {
    const view = document.getElementById("portfolioView");
    const edit = document.getElementById("portfolioEdit");
    const rows = document.getElementById("portfolioEditRows");

    view.style.display = "none";
    edit.style.display = "block";
    rows.innerHTML = "";

    try {
        const response = await fetch(`${API_BASE}/portfolio`);
        const data = await response.json();
        const positions = data.positions || {};
        const avgPrices = data.avg_prices || {};
        const tickers = Object.keys(positions);

        if (tickers.length === 0) {
            addPortfolioRow();
        } else {
            tickers.forEach(t => {
                addPortfolioRow(t, positions[t], avgPrices[t] || 0);
            });
        }
    } catch (error) {
        addPortfolioRow();
    }
}


function addPortfolioRow(ticker = "", qty = "", price = "") {
    const rows = document.getElementById("portfolioEditRows");
    const div = document.createElement("div");
    div.className = "portfolio-edit-row";
    div.innerHTML = `
        <input type="text" placeholder="Тикер" value="${ticker}" data-field="ticker">
        <input type="number" placeholder="Кол-во" value="${qty}" data-field="qty" step="1">
        <input type="number" placeholder="Средняя цена" value="${price}" data-field="price" step="0.01">
        <button class="remove-btn" onclick="this.parentElement.remove()">✕</button>
    `;
    rows.appendChild(div);
}


function cancelPortfolioEdit() {
    document.getElementById("portfolioView").style.display = "block";
    document.getElementById("portfolioEdit").style.display = "none";
}


async function savePortfolio() {
    const rows = document.querySelectorAll("#portfolioEditRows .portfolio-edit-row");
    const positions = {};
    const avgPrices = {};

    rows.forEach(row => {
        const ticker = row.querySelector('[data-field="ticker"]').value.trim().toUpperCase();
        const qty = parseFloat(row.querySelector('[data-field="qty"]').value);
        const price = parseFloat(row.querySelector('[data-field="price"]').value);

        if (ticker && !isNaN(qty) && qty > 0) {
            positions[ticker] = qty;
            avgPrices[ticker] = isNaN(price) ? 0 : price;
        }
    });

    if (Object.keys(positions).length === 0) {
        alert("Добавьте хотя бы одну позицию");
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/portfolio`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ positions, avg_prices: avgPrices })
        });

        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        document.getElementById("portfolioView").style.display = "block";
        document.getElementById("portfolioEdit").style.display = "none";
        await loadPortfolio();
    } catch (error) {
        alert("Ошибка сохранения: " + error.message);
    }
}


// ============================================================================
// Cooldown
// ============================================================================

async function checkCooldownStatus() {
    try {
        const response = await fetch(`${API_BASE}/optimize/cooldown`);
        const data = await response.json();
        updateCooldownUI(data);
        return data;
    } catch (error) {
        return { cooldown_active: false, hours_left: 0 };
    }
}


function updateCooldownUI(data) {
    const indicator = document.getElementById("cooldownIndicator");
    if (!indicator) return;

    if (data.cooldown_active) {
        const hours = data.hours_left.toFixed(1);
        indicator.style.display = "block";
        indicator.innerHTML = `⏳ Следующая оптимизация через <strong>${hours} ч.</strong>`;
    } else {
        indicator.style.display = "none";
    }
}


// ============================================================================
// Модалки
// ============================================================================

function showModal(html, buttons) {
    const old = document.getElementById("customModal");
    if (old) old.remove();

    const modal = document.createElement("div");
    modal.id = "customModal";
    modal.style.cssText = `
        position: fixed; top: 0; left: 0; width: 100%; height: 100%;
        background: rgba(0,0,0,0.5); z-index: 9999;
        display: flex; align-items: center; justify-content: center;
    `;

    const box = document.createElement("div");
    box.style.cssText = `
        background: white; border-radius: 12px; padding: 24px;
        max-width: 500px; width: 90%; box-shadow: 0 10px 40px rgba(0,0,0,0.2);
    `;
    box.innerHTML = html;

    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display: flex; gap: 12px; margin-top: 20px; justify-content: flex-end;";

    buttons.forEach(btn => {
        const b = document.createElement("button");
        b.textContent = btn.text;
        b.style.cssText = `
            padding: 10px 20px; border-radius: 6px; font-weight: 600;
            cursor: pointer; font-size: 14px; border: none;
            ${btn.style || "background: #e5e7eb; color: #374151;"}
        `;
        b.onclick = () => {
            modal.remove();
            if (btn.onClick) btn.onClick();
        };
        btnRow.appendChild(b);
    });

    box.appendChild(btnRow);
    modal.appendChild(box);
    document.body.appendChild(modal);
}


function showCooldownModal(hoursLeft) {
    showModal(`
        <div style="font-size: 20px; font-weight: 700; color: #111; margin-bottom: 12px;">
            ⏳ Cooldown активен
        </div>
        <div style="color: #475569; line-height: 1.6; font-size: 14px;">
            Следующая оптимизация будет доступна через
            <strong>${hoursLeft.toFixed(1)} ч.</strong>
            <br><br>
            Частая оптимизация приводит к излишним сделкам и комиссиям брокера.
            Мы ограничили частоту, чтобы защитить вашу прибыль.
        </div>
    `, [
        { text: "Подождать", style: "background: #2563eb; color: white;" },
        {
            text: "Снять КД",
            style: "background: #fee2e2; color: #dc2626;",
            onClick: () => showForceConfirmModal()
        }
    ]);
}


function showForceConfirmModal() {
    showModal(`
        <div style="font-size: 20px; font-weight: 700; color: #dc2626; margin-bottom: 12px;">
            ⚠️ Принудительная оптимизация
        </div>
        <div style="color: #475569; line-height: 1.6; font-size: 14px;">
            Вы собираетесь обойти cooldown и запустить оптимизацию раньше срока.
            <br><br>
            <strong>Риск:</strong> каждая дополнительная оптимизация может привести к
            покупке/продаже акций. За каждую сделку брокер берёт комиссию
            <strong>до 0.04%</strong> от суммы. При частых сделках комиссии
            могут <strong>полностью съесть вашу прибыль</strong>.
            <br><br>
            Рекомендуем запускать оптимизацию <strong>не чаще раза в сутки</strong>.
        </div>
    `, [
        { text: "Отмена", style: "background: #e5e7eb; color: #374151;" },
        {
            text: "Понимаю риск, продолжить",
            style: "background: #dc2626; color: white;",
            onClick: () => startOptimization(true)
        }
    ]);
}


// ============================================================================
// Кастомные профили
// ============================================================================

async function saveCustomProfile() {
    const nameInput = document.getElementById("profileName");
    const descInput = document.getElementById("profileDescription");
    const name = nameInput.value.trim();
    const description = descInput ? descInput.value.trim() : "";

    if (!name || name.length < 2) {
        alert("Введите название профиля (минимум 2 символа)");
        return;
    }

    const profileName = name.toLowerCase()
        .replace(/[^a-zа-я0-9\s]/g, "")
        .replace(/\s+/g, "_")
        .trim();

    if (!profileName) {
        alert("Название должно содержать буквы или цифры");
        return;
    }

    const rawWeight = parseFloat(document.getElementById("maxAssetWeight").value);
    const cooldownHours = parseFloat(document.getElementById("cooldownHours")?.value || 24);
    const noTradeThreshold = parseFloat(document.getElementById("noTradeThreshold")?.value || 5) / 100.0;
    const maxTurnover = parseFloat(document.getElementById("maxTurnover")?.value || 20) / 100.0;

    const payload = {
        name: profileName,
        display_name: name,
        description: description,
        model_name: document.getElementById("modelName").value,
        optimisation_strategy: document.getElementById("strategyName").value,
        max_asset_weight: rawWeight / 100.0,
        risk_aversion: parseFloat(document.getElementById("riskAversion").value),
        days_to_forecast: 30,
        cooldown_hours: cooldownHours,
        no_trade_threshold: noTradeThreshold,
        max_turnover: maxTurnover,
    };

    try {
        const response = await fetch(`${API_BASE}/profiles/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || "Ошибка сохранения");
        }

        alert(`Профиль "${name}" сохранён!`);
        nameInput.value = "";
        if (descInput) descInput.value = "";
        await loadProfiles();
    } catch (error) {
        alert("Ошибка: " + error.message);
    }
}


async function deleteCustomProfile(profileName, displayName) {
    const label = displayName || profileName;
    if (!confirm(`Удалить профиль "${label}"?`)) return;

    try {
        const response = await fetch(`${API_BASE}/profiles/${profileName}`, {
            method: "DELETE"
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || "Ошибка удаления");
        }

        await loadProfiles();
    } catch (error) {
        alert("Ошибка: " + error.message);
    }
}


// ============================================================================
// Оптимизация
// ============================================================================

async function startOptimization(force = false) {
    const calcBtn = document.getElementById("calculateBtn");
    const loader = document.getElementById("loader");
    const resultsBox = document.getElementById("resultsBox");

    resultsBox.style.display = "none";

    const checkboxes = document.querySelectorAll('input[name="tickers"]:checked');
    const selectedTickers = Array.from(checkboxes).map(cb => cb.value);

    if (selectedTickers.length === 0) {
        alert("Пожалуйста, выберите хотя бы один тикер.");
        return;
    }

    let payload = {
        selected_tickers: selectedTickers,
        force: force
    };

    if (selectedProfile) {
        payload.profile_name = selectedProfile;
    } else {
        const rawWeight = parseFloat(document.getElementById("maxAssetWeight").value);
        const cooldownHours = parseFloat(document.getElementById("cooldownHours")?.value || 24);
        const noTradeThreshold = parseFloat(document.getElementById("noTradeThreshold")?.value || 5) / 100.0;
        const maxTurnover = parseFloat(document.getElementById("maxTurnover")?.value || 20) / 100.0;

        payload.model_name = document.getElementById("modelName").value;
        payload.optimisation_strategy = document.getElementById("strategyName").value;
        payload.risk_aversion = parseFloat(document.getElementById("riskAversion").value);
        payload.days_to_forecast = 30;
        payload.max_asset_weight = rawWeight / 100.0;
        payload.cooldown_hours = cooldownHours;
        payload.no_trade_threshold = noTradeThreshold;
        payload.max_turnover = maxTurnover;
    }

    try {
        calcBtn.disabled = true;
        loader.style.display = "block";

        const response = await fetch(`${API_BASE}/optimize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (response.status === 429) {
            const error = await response.json();
            const detail = error.detail || {};

            calcBtn.disabled = false;
            loader.style.display = "none";
            showCooldownModal(detail.hours_left || 0);
            return;
        }

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail?.message || error.detail || "Ошибка при отправке задачи");
        }

        const data = await response.json();
        pollTaskStatus(data.task_id);
        setTimeout(checkCooldownStatus, 1000);

    } catch (error) {
        alert(error.message);
        calcBtn.disabled = false;
        loader.style.display = "none";
    }
}


function pollTaskStatus(taskId) {
    const loader = document.getElementById("loader");
    const calcBtn = document.getElementById("calculateBtn");

    const interval = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/tasks/${taskId}`);
            const task = await response.json();

            if (task.status === "SUCCESS") {
                clearInterval(interval);
                displayResults(task.result.weights, task.result);
                calcBtn.disabled = false;
                loader.style.display = "none";
            } else if (task.status === "FAILURE") {
                clearInterval(interval);
                alert("Ошибка воркера: " + task.error);
                calcBtn.disabled = false;
                loader.style.display = "none";
            }
        } catch (error) {
            clearInterval(interval);
            alert(error.message);
            calcBtn.disabled = false;
            loader.style.display = "none";
        }
    }, 1500);
}


function displayResults(weights, meta = {}) {
    const resultsBody = document.getElementById("resultsBody");
    const resultsBox = document.getElementById("resultsBox");
    resultsBody.innerHTML = "";

    // === ПРЕДУПРЕЖДЕНИЕ О FALLBACK ===
    const oldWarning = document.getElementById("fallbackWarning");
    if (oldWarning) oldWarning.remove();

    if (meta.fallback_used) {
        const warning = document.createElement("div");
        warning.id = "fallbackWarning";
        warning.style.cssText = `
            background: #fef3c7; border: 1px solid #fbbf24; color: #92400e;
            padding: 12px 16px; border-radius: 6px; margin-bottom: 16px;
            font-size: 14px; line-height: 1.5;
        `;
        warning.innerHTML = `
            ⚠️ <strong>Оптимизатор не смог найти оптимальное решение.</strong><br>
            <span style="font-size: 12px;">
                Причина: ${meta.optimiser_message || "все прогнозы хуже безрисковой ставки"}.<br>
                Применены веса <strong>пропорционально прогнозам доходности</strong>.
            </span>
        `;
        resultsBox.insertBefore(warning, resultsBox.firstChild);
    }

    // === TURNOVER ИНФОРМАЦИЯ ===
    const oldTurnover = document.getElementById("turnoverInfo");
    if (oldTurnover) oldTurnover.remove();

    const turnover = meta.turnover || 0;
    const rebalanceSkipped = meta.rebalance_skipped || false;
    const interpolated = meta.interpolated || false;
    const hasCurrentPortfolio = meta.current_weights && Object.keys(meta.current_weights).length > 0;

    if (hasCurrentPortfolio) {
        let turnoverBlock = "";

        if (rebalanceSkipped) {
            turnoverBlock = `
                <div style="background: #f0f9ff; border: 1px solid #7dd3fc; color: #0369a1;
                            padding: 12px 16px; border-radius: 6px; margin-bottom: 16px;
                            font-size: 14px; line-height: 1.5;">
                    ⏸️ <strong>Ребалансировка не требуется</strong><br>
                    <span style="font-size: 12px;">
                        Отклонение от целевых весов: <strong>${(turnover * 100).toFixed(2)}%</strong> —
                        ниже порога. Сделки не выполняются, экономия на комиссиях.
                    </span>
                </div>
            `;
        } else if (interpolated) {
            turnoverBlock = `
                <div style="background: #fef3c7; border: 1px solid #fbbf24; color: #92400e;
                            padding: 12px 16px; border-radius: 6px; margin-bottom: 16px;
                            font-size: 14px; line-height: 1.5;">
                    ⚡ <strong>Частичная ребалансировка</strong><br>
                    <span style="font-size: 12px;">
                        Отклонение было слишком большим — веса плавно интерполированы.
                        Turnover после интерполяции: <strong>${(turnover * 100).toFixed(2)}%</strong>.
                    </span>
                </div>
            `;
        } else {
            const estimatedCommission = estimateCommission(weights, meta.current_weights);
            turnoverBlock = `
                <div style="background: #f0fdf4; border: 1px solid #86efac; color: #166534;
                            padding: 12px 16px; border-radius: 6px; margin-bottom: 16px;
                            font-size: 14px; line-height: 1.5;">
                    ✅ <strong>Ребалансировка выполнена</strong><br>
                    <span style="font-size: 12px;">
                        Turnover: <strong>${(turnover * 100).toFixed(2)}%</strong> ·
                        Ожидаемая комиссия: ~<strong>${estimatedCommission.toFixed(2)} ₽</strong>
                    </span>
                </div>
            `;
        }

        const turnoverDiv = document.createElement("div");
        turnoverDiv.id = "turnoverInfo";
        turnoverDiv.innerHTML = turnoverBlock;
        resultsBox.insertBefore(turnoverDiv, resultsBox.firstChild);
    }

    // === ЗАГОЛОВОК С МЕТАДАННЫМИ ===
    const oldInfo = document.getElementById("resultInfo");
    if (oldInfo) oldInfo.remove();

    if (meta.applied_model) {
        const info = document.createElement("div");
        info.id = "resultInfo";
        info.style.cssText = "font-size: 12px; color: #64748b; margin-bottom: 12px;";
        const rate = meta.risk_free_rate ? (meta.risk_free_rate * 100).toFixed(1) : "—";
        info.textContent = `Модель: ${meta.applied_model} | Стратегия: ${meta.applied_strategy} | Ставка ЦБ: ${rate}%`;
        resultsBox.insertBefore(info, resultsBox.querySelector(".results-table"));
    }

    for (const [ticker, weight] of Object.entries(weights)) {
        const percentage = (weight * 100).toFixed(2);
        const row = `<tr><td><strong>${ticker}</strong></td><td>${percentage}%</td></tr>`;
        resultsBody.innerHTML += row;
    }

    const cashWeight = meta.cash_weight || 0;
    if (cashWeight > 0.0001) {
        const cashPercentage = (cashWeight * 100).toFixed(2);
        const cashRow = `<tr style="background: #f9fafb; border-top: 2px solid #e5e7eb;">
            <td><strong>💵 Наличные</strong></td>
            <td style="color: #6b7280;">${cashPercentage}%</td>
        </tr>`;
        resultsBody.innerHTML += cashRow;
    }

    resultsBox.style.display = "block";
}


// ============================================================================
// Оценка комиссии
// ============================================================================

function estimateCommission(targetWeights, currentWeights) {
    const ASSUMED_PORTFOLIO_VALUE = 100000;
    const COMMISSION_RATE = 0.0004;

    let turnoverSum = 0;
    const allTickers = new Set([
        ...Object.keys(targetWeights || {}),
        ...Object.keys(currentWeights || {})
    ]);

    allTickers.forEach(ticker => {
        const target = targetWeights[ticker] || 0;
        const current = currentWeights[ticker] || 0;
        turnoverSum += Math.abs(target - current);
    });

    const turnoverValue = (turnoverSum / 2) * ASSUMED_PORTFOLIO_VALUE;
    return turnoverValue * COMMISSION_RATE;
}


// ============================================================================
// История
// ============================================================================

function toggleHistory() {
    const section = document.getElementById("historySection");
    const arrow = document.getElementById("historyArrow");

    if (section.style.display === "none") {
        section.style.display = "block";
        arrow.textContent = "▾";
        loadHistory();
    } else {
        section.style.display = "none";
        arrow.textContent = "▸";
    }
}


async function loadHistory() {
    const content = document.getElementById("historyContent");
    content.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 20px;">Загрузка...</div>';

    try {
        const response = await fetch(`${API_BASE}/history?limit=20`);
        const data = await response.json();

        if (!data.history || data.history.length === 0) {
            content.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 20px;">История пуста.</div>';
            return;
        }

        let html = '<table style="width: 100%; border-collapse: collapse; font-size: 13px;">';
        html += `<thead><tr style="border-bottom: 2px solid #e2e8f0;">
            <th style="padding: 8px; text-align: left;">Время</th>
            <th style="padding: 8px; text-align: left;">Модель</th>
            <th style="padding: 8px; text-align: left;">Портфель</th>
            <th style="padding: 8px; text-align: left;">Статус</th>
        </tr></thead><tbody>`;

        data.history.forEach(run => {
            const date = new Date(run.run_at).toLocaleString("ru-RU", {
                day: "2-digit", month: "2-digit", year: "2-digit",
                hour: "2-digit", minute: "2-digit"
            });

            const weightsStr = Object.entries(run.weights)
                .map(([ticker, w]) => `${ticker} ${(w * 100).toFixed(0)}%`)
                .join(", ");
            const cashStr = run.cash_weight > 0.001
                ? `, 💵 ${(run.cash_weight * 100).toFixed(0)}%` : "";
            const statusIcon = run.fallback_used ? "⚠️" : "✅";
            const statusText = run.fallback_used ? "fallback" : "оптимум";

            html += `<tr style="border-bottom: 1px solid #f1f5f9;">
                <td style="padding: 8px; color: #64748b; white-space: nowrap;">${date}</td>
                <td style="padding: 8px; color: #475569;">${run.model_name}</td>
                <td style="padding: 8px; color: #111;">${weightsStr}${cashStr}</td>
                <td style="padding: 8px;">${statusIcon} ${statusText}</td>
            </tr>`;
        });

        html += '</tbody></table>';
        content.innerHTML = html;
    } catch (error) {
        content.innerHTML = `<div style="text-align: center; color: #dc2626; padding: 20px;">Ошибка: ${error.message}</div>`;
    }
}


// ============================================================================
// Инициализация
// ============================================================================

document.addEventListener("DOMContentLoaded", () => {
    loadProfiles();
    loadModels();
    checkCooldownStatus();
});