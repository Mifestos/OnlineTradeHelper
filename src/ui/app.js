// src/ui/app.js
const API_BASE = "http://localhost:8000/api/v1";

// Глобальное состояние: выбранный профиль (или null для ручного)
let selectedProfile = null;


// ============================================================================
// Загрузка профилей
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
            
            // Кнопка удаления — только для кастомных профилей
            const deleteButton = profile.is_custom 
                ? `<button onclick="event.stopPropagation(); deleteCustomProfile('${profile.name}', '${profile.display_name}')" 
                    style="position: absolute; top: 8px; right: 8px; width: 32px; height: 32px;
                        background: #fee2e2; border: 1px solid #fca5a5;
                        color: #dc2626; font-size: 20px; font-weight: 900;
                        cursor: pointer; line-height: 1; display: flex;
                        align-items: center; justify-content: center;
                        border-radius: 6px; transition: all 0.15s; padding: 0;"
                    onmouseover="this.style.background='#fecaca';"
                    onmouseout="this.style.background='#fee2e2';"
                    title="Удалить профиль">✕</button>` 
                : '';
            
            card.innerHTML = `
                ${deleteButton}
                <div class="title">${profile.display_name}</div>
                <div class="desc">${profile.description}</div>
                <div class="params">Модель: ${profile.parameters.model_name} | Макс. доля: ${maxWeight}%</div>
            `;
            
            card.addEventListener("click", () => selectProfile(profile.name));
            grid.appendChild(card);
        });
        
        // По умолчанию выбираем balanced
        const defaultCard = grid.querySelector('[data-profile="balanced"]');
        if (defaultCard) {
            defaultCard.click();
        }
        
        console.log(`[UI] Загружено профилей: ${data.profiles.length}`);
    } catch (error) {
        console.error("[UI] Ошибка загрузки профилей:", error);
        grid.innerHTML = '<div class="profile-card">Ошибка загрузки профилей</div>';
    }
}


// ============================================================================
// Выбор профиля
// ============================================================================

function selectProfile(profileName) {
    selectedProfile = profileName;
    
    document.querySelectorAll(".profile-card").forEach(card => {
        card.classList.toggle("selected", card.dataset.profile === profileName);
    });
    
    console.log(`[UI] Выбран профиль: ${profileName}`);
}


// ============================================================================
// Раскрытие/сворачивание ручных настроек
// ============================================================================

function toggleManual() {
    const section = document.getElementById("manualSection");
    const saveBox = document.getElementById("saveProfileBox");
    section.classList.toggle("open");
    
    if (section.classList.contains("open")) {
        selectedProfile = null;
        document.querySelectorAll(".profile-card").forEach(card => card.classList.remove("selected"));
        if (saveBox) saveBox.style.display = "block";
        console.log("[UI] Ручной режим");
    } else {
        if (saveBox) saveBox.style.display = "none";
    }
}


// ============================================================================
// Загрузка моделей
// ============================================================================

async function loadModels() {
    const select = document.getElementById("modelName");
    
    try {
        const response = await fetch(`${API_BASE}/models`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const data = await response.json();
        
        select.innerHTML = "";
        data.models.forEach(model => {
            const option = document.createElement("option");
            option.value = model.name;
            option.textContent = model.description;
            select.appendChild(option);
        });
        
        console.log(`[UI] Загружено моделей: ${data.models.length}`);
    } catch (error) {
        console.error("[UI] Ошибка загрузки моделей:", error);
        select.innerHTML = '<option value="prophet">Prophet (fallback)</option>';
    }
}


// ============================================================================
// Сохранение кастомного профиля
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
    
    // Генерируем латинский profile_name из display_name
    const profileName = name.toLowerCase()
        .replace(/[^a-zа-я0-9\s]/g, "")
        .replace(/\s+/g, "_")
        .trim();
    
    if (!profileName) {
        alert("Название должно содержать буквы или цифры");
        return;
    }
    
    const rawWeight = parseFloat(document.getElementById("maxAssetWeight").value);
    
    const payload = {
        name: profileName,
        display_name: name,
        description: description,
        model_name: document.getElementById("modelName").value,
        optimisation_strategy: document.getElementById("strategyName").value,
        max_asset_weight: rawWeight / 100.0,
        risk_aversion: parseFloat(document.getElementById("riskAversion").value),
        days_to_forecast: 30,
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
        
        alert(`✅ Профиль "${name}" сохранён!`);
        
        nameInput.value = "";
        if (descInput) descInput.value = "";
        
        await loadProfiles();
    } catch (error) {
        alert("Ошибка: " + error.message);
    }
}


// ============================================================================
// Удаление кастомного профиля
// ============================================================================

async function deleteCustomProfile(profileName, displayName) {
    const label = displayName || profileName;
    
    if (!confirm(`Удалить профиль "${label}"?`)) {
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/profiles/${profileName}`, {
            method: "DELETE"
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || "Ошибка удаления");
        }
        
        console.log(`[UI] Профиль "${profileName}" удалён`);
        await loadProfiles();
    } catch (error) {
        alert("Ошибка: " + error.message);
    }
}


// ============================================================================
// Запуск оптимизации
// ============================================================================

async function startOptimization() {
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
    
    let payload = { selected_tickers: selectedTickers };
    
    if (selectedProfile) {
        payload.profile_name = selectedProfile;
        console.log(`[UI] Запуск с профилем: ${selectedProfile}`);
    } else {
        const rawWeight = parseFloat(document.getElementById("maxAssetWeight").value);
        payload.model_name = document.getElementById("modelName").value;
        payload.optimisation_strategy = document.getElementById("strategyName").value;
        payload.risk_aversion = parseFloat(document.getElementById("riskAversion").value);
        payload.days_to_forecast = 30;
        payload.max_asset_weight = rawWeight / 100.0;
        console.log("[UI] Запуск с ручными настройками:", payload);
    }
    
    try {
        calcBtn.disabled = true;
        loader.style.display = "block";
        
        const response = await fetch(`${API_BASE}/optimize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || "Ошибка при отправке задачи");
        }
        
        const data = await response.json();
        pollTaskStatus(data.task_id);
    } catch (error) {
        alert(error.message);
        calcBtn.disabled = false;
        loader.style.display = "none";
    }
}


// ============================================================================
// Опрос статуса
// ============================================================================

function pollTaskStatus(taskId) {
    const loader = document.getElementById("loader");
    const calcBtn = document.getElementById("calculateBtn");
    
    const interval = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/tasks/${taskId}`);
            if (!response.ok) throw new Error("Ошибка опроса статуса");
            
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


// ============================================================================
// Отображение результатов
// ============================================================================

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
    
    // === ТАБЛИЦА С ВЕСАМИ ===
    for (const [ticker, weight] of Object.entries(weights)) {
        const percentage = (weight * 100).toFixed(2);
        const row = `<tr>
            <td><strong>${ticker}</strong></td>
            <td>${percentage}%</td>
        </tr>`;
        resultsBody.innerHTML += row;
    }
    
    // === СТРОКА "НАЛИЧНЫЕ" ===
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
// История оптимизаций
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
    content.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 20px;">⏳ Загрузка...</div>';
    
    try {
        const response = await fetch(`${API_BASE}/history?limit=20`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const data = await response.json();
        
        if (!data.history || data.history.length === 0) {
            content.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 20px;">История пуста. Запустите оптимизацию — и она появится здесь.</div>';
            return;
        }
        
        let html = '<table style="width: 100%; border-collapse: collapse; font-size: 13px;">';
        html += `
            <thead>
                <tr style="border-bottom: 2px solid #e2e8f0;">
                    <th style="padding: 8px; text-align: left;">Время</th>
                    <th style="padding: 8px; text-align: left;">Модель</th>
                    <th style="padding: 8px; text-align: left;">Портфель</th>
                    <th style="padding: 8px; text-align: left;">Статус</th>
                </tr>
            </thead>
            <tbody>
        `;
        
        data.history.forEach(run => {
            const date = new Date(run.run_at).toLocaleString("ru-RU", {
                day: "2-digit", month: "2-digit", year: "2-digit",
                hour: "2-digit", minute: "2-digit"
            });
            
            const weightsStr = Object.entries(run.weights)
                .map(([ticker, w]) => `${ticker} ${(w * 100).toFixed(0)}%`)
                .join(", ");
            
            const cashStr = run.cash_weight > 0.001 
                ? `, 💵 ${(run.cash_weight * 100).toFixed(0)}%` 
                : "";
            
            const statusIcon = run.fallback_used ? "⚠️" : "✅";
            const statusText = run.fallback_used ? "fallback" : "оптимум";
            
            html += `
                <tr style="border-bottom: 1px solid #f1f5f9;">
                    <td style="padding: 8px; color: #64748b; white-space: nowrap;">${date}</td>
                    <td style="padding: 8px; color: #475569;">${run.model_name}</td>
                    <td style="padding: 8px; color: #111;">${weightsStr}${cashStr}</td>
                    <td style="padding: 8px;">${statusIcon} ${statusText}</td>
                </tr>
            `;
        });
        
        html += '</tbody></table>';
        content.innerHTML = html;
        
        console.log(`[UI] Загружено ${data.history.length} записей истории`);
    } catch (error) {
        console.error("[UI] Ошибка загрузки истории:", error);
        content.innerHTML = `<div style="text-align: center; color: #dc2626; padding: 20px;">Ошибка: ${error.message}</div>`;
    }
}


// ============================================================================
// Инициализация
// ============================================================================

document.addEventListener("DOMContentLoaded", () => {
    loadProfiles();
    loadModels();
});