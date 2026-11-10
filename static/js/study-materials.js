(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const data = (id) => {
    const node = el(id);
    return node ? JSON.parse(node.textContent) : [];
  };
  const setSource = (button, value) => {
    const available = value !== null && value !== undefined && Number.isFinite(Number(value));
    button.hidden = !available;
    if (available) {
      const time = Math.floor(Number(value));
      const label = [Math.floor(time / 3600), Math.floor(time / 60) % 60, time % 60]
        .map((part) => String(part).padStart(2, "0")).join(":");
      button.dataset.seek = String(value);
      button.textContent = "Play from " + label;
    } else delete button.dataset.seek;
  };
  const questions = data("quiz-data");
  if (questions.length) {
    let index = 0, score = 0, answered = false;
    const showQuestion = () => {
      answered = false;
      el("quiz-progress").textContent = "Question " + (index + 1) + " of " + questions.length;
      el("quiz-question").textContent = questions[index].question;
      el("quiz-options").replaceChildren();
      for (const letter of ["A", "B", "C", "D"]) {
        const label = document.createElement("label");
        label.className = "quiz-option border rounded-3 p-3 d-flex align-items-start gap-3";
        const input = document.createElement("input");
        input.type = "radio";
        input.name = "quiz-answer";
        input.value = letter;
        input.className = "form-check-input flex-shrink-0";
        input.addEventListener("change", () => { el("quiz-check").disabled = false; });
        const text = document.createElement("span");
        text.className = "text-break";
        text.textContent = letter + ". " + questions[index]["option_" + letter.toLowerCase()];
        label.append(input, text);
        el("quiz-options").append(label);
      }
      el("quiz-feedback").hidden = true;
      el("quiz-explanation-area").hidden = true;
      el("quiz-check").disabled = true;
      el("quiz-next").disabled = true;
      el("quiz-next").textContent = index === questions.length - 1 ? "Show final score" : "Next question";
    };
    el("quiz-check").addEventListener("click", () => {
      const selected = document.querySelector('input[name="quiz-answer"]:checked');
      if (!selected || answered) return;
      answered = true;
      const correct = selected.value === questions[index].correct_option;
      if (correct) score++;
      document.querySelectorAll('input[name="quiz-answer"]').forEach((input) => {
        input.disabled = true;
        if (input.value === questions[index].correct_option) input.parentElement.classList.add("border-success", "bg-success-subtle");
        else if (input.checked) input.parentElement.classList.add("border-danger", "bg-danger-subtle");
      });
      el("quiz-feedback").className = "alert mt-3 " + (correct ? "alert-success" : "alert-danger");
      el("quiz-feedback").textContent = correct ? "Correct!" : "Not quite. The correct option is " + questions[index].correct_option + ".";
      el("quiz-feedback").hidden = false;
      el("quiz-explanation").textContent = questions[index].explanation;
      el("quiz-evidence").textContent = questions[index].source_excerpt;
      setSource(el("quiz-source"), questions[index].source_timestamp);
      el("quiz-explanation-area").hidden = false;
      el("quiz-check").disabled = true;
      el("quiz-next").disabled = false;
    });
    el("quiz-next").addEventListener("click", () => {
      if (!answered) return;
      if (index === questions.length - 1) {
        el("quiz-runner").hidden = true;
        el("quiz-results").hidden = false;
        el("quiz-score").textContent = "You scored " + score + " / " + questions.length;
        el("quiz-restart").focus();
      } else {
        index++;
        showQuestion();
        el("quiz-question").tabIndex = -1;
        el("quiz-question").focus();
      }
    });
    el("quiz-restart").addEventListener("click", () => {
      index = 0;
      score = 0;
      el("quiz-results").hidden = true;
      el("quiz-runner").hidden = false;
      showQuestion();
    });
    showQuestion();
  }
  const cards = data("flashcard-data");
  if (cards.length) {
    let index = 0, flipped = false;
    const flip = (value) => {
      flipped = value;
      el("flashcard-flip").classList.toggle("is-flipped", flipped);
      el("flashcard-flip").setAttribute("aria-pressed", String(flipped));
      el("flashcard-flip").setAttribute("aria-label", flipped ? "Show flashcard question" : "Show flashcard answer");
      el("flashcard-front").setAttribute("aria-hidden", String(flipped));
      el("flashcard-back").setAttribute("aria-hidden", String(!flipped));
    };
    const showCard = () => {
      flip(false);
      el("flashcard-front").textContent = cards[index].front;
      el("flashcard-back").textContent = cards[index].back;
      el("flashcard-progress").textContent = "Card " + (index + 1) + " of " + cards.length;
      el("flashcard-previous").disabled = index === 0;
      el("flashcard-next").disabled = index === cards.length - 1;
      setSource(el("flashcard-source"), cards[index].source_timestamp);
    };
    el("flashcard-flip").addEventListener("click", () => flip(!flipped));
    el("flashcard-previous").addEventListener("click", () => { if (index > 0) { index--; showCard(); } });
    el("flashcard-next").addEventListener("click", () => { if (index < cards.length - 1) { index++; showCard(); } });
    showCard();
  }
  el("study-generation-form")?.addEventListener("submit", (event) => {
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = "Generating locally…";
    el("study-generation-status").textContent = "Creating validated quiz questions and flashcards. This can take several minutes.";
  });
})();
