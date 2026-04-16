function setResult(el, message, isError = false) {
  el.textContent = message;
  el.classList.toggle("error", isError);
}

async function readError(response) {
  try {
    const payload = await response.json();
    return JSON.stringify(payload, null, 2);
  } catch {
    return await response.text();
  }
}

const convertForm = document.getElementById("convert-form");
const convertResult = document.getElementById("convert-result");
const convertSubmit = document.getElementById("convert-submit");
const fileInput = document.getElementById("pdf-files");
const taskIdInput = document.getElementById("task-id");

convertForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const files = fileInput.files;
  if (!files || files.length === 0) {
    setResult(convertResult, "Select at least one PDF file.", true);
    return;
  }

  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }

  convertSubmit.disabled = true;
  setResult(convertResult, "Submitting conversion task...");

  try {
    const response = await fetch("/api/v1/conversions", {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      setResult(convertResult, await readError(response), true);
      return;
    }

    const data = await response.json();
    taskIdInput.value = data.task_id;
    setResult(convertResult, JSON.stringify(data, null, 2));
  } catch (error) {
    setResult(convertResult, String(error), true);
  } finally {
    convertSubmit.disabled = false;
  }
});

const statusForm = document.getElementById("status-form");
const statusResult = document.getElementById("status-result");

statusForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const taskId = taskIdInput.value.trim();
  if (!taskId) {
    setResult(statusResult, "Provide task_id.", true);
    return;
  }

  setResult(statusResult, "Loading status...");

  try {
    const response = await fetch(`/api/v1/conversions/${encodeURIComponent(taskId)}`);
    if (!response.ok) {
      setResult(statusResult, await readError(response), true);
      return;
    }

    const data = await response.json();
    setResult(statusResult, JSON.stringify(data, null, 2));
  } catch (error) {
    setResult(statusResult, String(error), true);
  }
});

const qaForm = document.getElementById("qa-form");
const qaResult = document.getElementById("qa-result");
const questionInput = document.getElementById("question");

qaForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const question = questionInput.value.trim();
  if (!question) {
    setResult(qaResult, "Question is required.", true);
    return;
  }

  setResult(qaResult, "Generating answer...");

  try {
    const response = await fetch("/api/v1/qa", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ question }),
    });

    if (!response.ok) {
      setResult(qaResult, await readError(response), true);
      return;
    }

    const data = await response.json();
    setResult(qaResult, data.answer || "<empty answer>");
  } catch (error) {
    setResult(qaResult, String(error), true);
  }
});
