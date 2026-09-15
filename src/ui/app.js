// src/ui/app.js
const API_BASE = "http://localhost:8000/api/v1";

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

    const payload = {
        selected_tickers: selectedTickers,
        model_name: document.getElementById("modelName").value,
        optimisation_strategy: document.getElementById("strategyName").value,
        risk_aversion: parseFloat(document.getElementById("riskAversion").value),
        days_to_forecast: 30
    };

    try {
        calcBtn.disabled = true;
        loader.style.display = "block";

        const response = await fetch(`${API_BASE}/optimize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (!response.ok) throw new Error("Ошибка при отправке задачи на бэкенд");
        
        const data = await response.json();
        pollTaskStatus(data.task_id);

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
            if (!response.ok) throw new Error("Ошибка опроса статуса задачи");
            
            const task = await response.json();
            
            if (task.status === "SUCCESS") {
                clearInterval(interval);
                displayResults(task.result.weights);
                calcBtn.disabled = false;
                loader.style.display = "none";
            } else if (task.status === "FAILURE") {
                clearInterval(interval);
                alert("Фоновый воркер Celery завершился с ошибкой: " + task.error);
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

function displayResults(weights) {
    const resultsBody = document.getElementById("resultsBody");
    const resultsBox = document.getElementById("resultsBox");
    resultsBody.innerHTML = "";
    
    for (const [ticker, weight] of Object.entries(weights)) {
        const percentage = (weight * 100).toFixed(2);
        const row = `<tr>
            <td><strong>${ticker}</strong></td>
            <td>${percentage}%</td>
        </tr>`;
        resultsBody.innerHTML += row;
    }
    
    resultsBox.style.display = "block";
}
