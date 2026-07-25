const tg = window.Telegram?.WebApp;
const app = document.querySelector("#app");
const sheetRoot = document.querySelector("#sheet-root");
const toastRoot = document.querySelector("#toast-root");

const state = {
  user: null,
  tournaments: [],
  current: null,
  tab: "bracket",
  maxPlayers: 32,
};

const formatMeta = {
  single_elimination: {
    label: "Single Elimination",
    short: "Single",
    icon: "🏆",
    description: "Одно поражение — и игрок выбывает",
  },
  double_elimination: {
    label: "Double Elimination",
    short: "Double",
    icon: "♻️",
    description: "Второй шанс через нижнюю сетку",
  },
  round_robin: {
    label: "Круговой турнир",
    short: "Круговой",
    icon: "🔁",
    description: "Каждый игрок встречается с каждым",
  },
};

const statusMeta = {
  registration: { label: "Набор игроков", className: "" },
  active: { label: "Идёт турнир", className: "live" },
  finished: { label: "Завершён", className: "done" },
};

const bracketNames = {
  winners: "Верхняя сетка",
  losers: "Нижняя сетка",
  grand_final: "Гранд-финал",
  grand_final_reset: "Переигровка",
};

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function initials(name = "") {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "TT";
}

function authHeaders() {
  const headers = { "Content-Type": "application/json" };
  if (tg?.initData) {
    headers["X-Telegram-Init-Data"] = tg.initData;
  } else if (["localhost", "127.0.0.1"].includes(location.hostname)) {
    headers["X-Dev-User-Id"] = "900000001";
  }
  return headers;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || "Не удалось выполнить действие");
  }
  return payload;
}

function haptic(type = "light") {
  tg?.HapticFeedback?.impactOccurred(type);
}

function notify(message, type = "") {
  toastRoot.innerHTML = `<div class="toast ${type}">${escapeHtml(message)}</div>`;
  window.setTimeout(() => {
    toastRoot.innerHTML = "";
  }, 2800);
}

function openSheet(content) {
  sheetRoot.innerHTML = `
    <div class="sheet-backdrop" data-action="close-sheet">
      <section class="sheet" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
        <div class="sheet-handle"></div>
        ${content}
      </section>
    </div>
  `;
  document.body.style.overflow = "hidden";
  sheetRoot.querySelector("input")?.focus();
}

function closeSheet() {
  sheetRoot.innerHTML = "";
  document.body.style.overflow = "";
}

function renderTopbar() {
  return `
    <header class="topbar">
      <div class="wordmark">
        <span class="wordmark-dot"></span>
        <span>TT Bracket</span>
      </div>
      <button class="avatar" data-action="profile" aria-label="Профиль">
        ${escapeHtml(initials(state.user?.display_name))}
      </button>
    </header>
  `;
}

function tournamentCard(tournament) {
  const status = statusMeta[tournament.status];
  return `
    <button class="tournament-card" data-tournament="${tournament.id}">
      <span>
        <span class="badge-row">
          <span class="badge ${status.className}">${status.label}</span>
          <span class="badge">${formatMeta[tournament.format]?.short || tournament.format}</span>
        </span>
        <span class="card-title">${escapeHtml(tournament.name)}</span>
        <span class="meta-row">
          <span class="badge">👥 ${tournament.player_count}</span>
          ${
            tournament.champion_name
              ? `<span class="badge done">🏆 ${escapeHtml(tournament.champion_name)}</span>`
              : ""
          }
        </span>
      </span>
      <span class="card-arrow">›</span>
    </button>
  `;
}

function renderHome() {
  state.current = null;
  state.tab = "bracket";
  const activeCount = state.tournaments.filter((item) => item.status === "active").length;
  const finishedCount = state.tournaments.filter((item) => item.status === "finished").length;
  app.innerHTML = `
    ${renderTopbar()}
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">ТУРНИРНЫЙ ЦЕНТР</p>
        <h1>Играй. Побеждай. Продвигайся.</h1>
        <p>Создавайте турниры, следите за сеткой и фиксируйте результаты прямо во время игры.</p>
      </div>
      <div class="stat-strip">
        <div class="stat"><strong>${activeCount}</strong><span>сейчас играют</span></div>
        <div class="stat"><strong>${finishedCount}</strong><span>турниров завершено</span></div>
      </div>
    </section>
    <div class="section-head">
      <h2>Турниры</h2>
      <button class="text-button" data-action="refresh">Обновить</button>
    </div>
    ${
      state.tournaments.length
        ? `<section class="tournament-grid">${state.tournaments.map(tournamentCard).join("")}</section>`
        : `<section class="empty-state">
            <span class="empty-icon">🏓</span>
            Здесь появится ваш первый турнир.<br />Создайте его за пару касаний.
          </section>`
    }
    <button class="floating-button" data-action="create-tournament">
      <span>＋</span> Новый турнир
    </button>
  `;
  updateTelegramBackButton(false);
  updateUrl(null);
}

function updateUrl(tournamentId) {
  const url = new URL(location.href);
  if (tournamentId) {
    url.searchParams.set("tournament", tournamentId);
  } else {
    url.searchParams.delete("tournament");
  }
  history.replaceState({}, "", url);
}

function updateTelegramBackButton(visible) {
  if (!tg?.BackButton) return;
  if (visible) tg.BackButton.show();
  else tg.BackButton.hide();
}

function roundTitle(round, totalRounds) {
  const remaining = totalRounds - round;
  if (remaining === 0) return "Финал";
  if (remaining === 1) return "Полуфинал";
  if (remaining === 2) return "Четвертьфинал";
  return `1/${2 ** remaining} финала`;
}

function matchCard(match, canManage, finalColumn = false) {
  const selectable = canManage && match.status === "ready";
  const winner1 = match.winner_id && match.winner_id === match.player1_id;
  const winner2 = match.winner_id && match.winner_id === match.player2_id;
  const stateLabel = {
    ready: "Готов к игре",
    finished: "Матч завершён",
    bye: "Проход без игры",
    pending: "Ожидает соперников",
  }[match.status];
  return `
    <article
      class="match-card ${selectable ? "selectable" : ""}"
      ${selectable ? `data-match="${match.id}"` : ""}
      ${finalColumn ? 'data-final="true"' : ""}
    >
      <div class="player-slot ${winner1 ? "winner" : ""}">
        <span>${escapeHtml(match.player1_name || "Ожидается")}</span>
        <span>${winner1 ? "✓" : ""}</span>
      </div>
      <div class="player-slot ${winner2 ? "winner" : ""}">
        <span>${escapeHtml(match.player2_name || (match.status === "bye" ? "Пропуск" : "Ожидается"))}</span>
        <span>${winner2 ? "✓" : ""}</span>
      </div>
      <div class="match-state ${match.status === "ready" ? "ready" : ""}">
        <span>${stateLabel}</span><span>#${match.position}</span>
      </div>
    </article>
  `;
}

function renderSingleBracket(payload) {
  const matches = payload.matches;
  if (!matches.length) return renderWaitingBracket(payload);
  const totalRounds = Math.max(...matches.map((match) => match.round_number));
  const columns = [];
  for (let round = 1; round <= totalRounds; round += 1) {
    const roundMatches = matches.filter((match) => match.round_number === round);
    columns.push(`
      <section class="round-column">
        <div class="round-heading">
          ${roundTitle(round, totalRounds)}
          <span>${roundMatches.length}</span>
        </div>
        <div class="match-stack">
          ${roundMatches
            .map((match) => matchCard(match, payload.tournament.can_manage, round === totalRounds))
            .join("")}
        </div>
      </section>
    `);
  }
  return `<div class="bracket-wrap"><div class="bracket-scroll">${columns.join("")}</div></div>`;
}

function doubleLane(payload, title, className, brackets) {
  const filtered = payload.matches.filter((match) => brackets.includes(match.bracket));
  if (!filtered.length) return "";
  const stages = [...new Set(filtered.map((match) => match.round_number))];
  return `
    <section class="lane ${className}">
      <h3 class="lane-title">${title}</h3>
      <div class="bracket-wrap">
        <div class="bracket-scroll">
          ${stages
            .map((stage) => {
              const stageMatches = filtered.filter((match) => match.round_number === stage);
              return `
                <section class="round-column">
                  <div class="round-heading">Этап ${stage}<span>${stageMatches.length}</span></div>
                  <div class="match-stack">
                    ${stageMatches
                      .map((match) => matchCard(match, payload.tournament.can_manage))
                      .join("")}
                  </div>
                </section>
              `;
            })
            .join("")}
        </div>
      </div>
    </section>
  `;
}

function renderDoubleBracket(payload) {
  if (!payload.matches.length) return renderWaitingBracket(payload);
  return `
    ${doubleLane(payload, "Верхняя сетка", "winners", ["winners"])}
    ${doubleLane(payload, "Нижняя сетка", "losers", ["losers"])}
    ${doubleLane(payload, "Финальная серия", "finals", ["grand_final", "grand_final_reset"])}
  `;
}

function renderRoundRobin(payload) {
  if (!payload.matches.length) return renderWaitingBracket(payload);
  const rounds = [...new Set(payload.matches.map((match) => match.round_number))];
  return `
    <div class="section-head"><h3>Турнирная таблица</h3></div>
    <table class="standings">
      <thead>
        <tr><th>#</th><th>Игрок</th><th class="number">И</th><th class="number">В</th><th class="number">П</th></tr>
      </thead>
      <tbody>
        ${payload.standings
          .map(
            (row) => `
              <tr>
                <td class="rank">${row.rank}</td>
                <td>${escapeHtml(row.display_name)}</td>
                <td class="number">${row.played}</td>
                <td class="number">${row.wins}</td>
                <td class="number">${row.losses}</td>
              </tr>
            `,
          )
          .join("")}
      </tbody>
    </table>
    <div class="section-head"><h3>Расписание</h3></div>
    <div class="round-list">
      ${rounds
        .map((round) => {
          const matches = payload.matches.filter((match) => match.round_number === round);
          return `
            <section class="round-block">
              <h3>Тур ${round}</h3>
              ${matches.map((match) => matchCard(match, payload.tournament.can_manage)).join("")}
            </section>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderWaitingBracket(payload) {
  const format = formatMeta[payload.tournament.format];
  return `
    <section class="empty-state">
      <span class="empty-icon">${format.icon}</span>
      <strong>${format.label}</strong><br />
      Добавьте минимум двух игроков и запустите турнир.
    </section>
  `;
}

function renderPlayers(payload) {
  if (!payload.players.length) {
    return `<section class="empty-state"><span class="empty-icon">👥</span>Участников пока нет.</section>`;
  }
  return `
    <section class="player-list">
      ${payload.players
        .map(
          (player, index) => `
            <article class="player-row">
              <span class="player-number">${index + 1}</span>
              <span>
                <strong>${escapeHtml(player.display_name)}</strong>
                <small>
                  ${player.telegram_id ? "Зарегистрирован" : "Добавлен вручную"}
                  ${
                    payload.tournament.format === "double_elimination" &&
                    payload.tournament.status !== "registration"
                      ? ` · поражений: ${player.losses}`
                      : ""
                  }
                </small>
              </span>
              ${
                payload.tournament.can_manage && payload.tournament.status === "registration"
                  ? `<button class="remove-player" data-remove-player="${player.id}" aria-label="Удалить игрока">×</button>`
                  : ""
              }
            </article>
          `,
        )
        .join("")}
    </section>
  `;
}

function renderTournament() {
  const payload = state.current;
  const tournament = payload.tournament;
  const status = statusMeta[tournament.status];
  const format = formatMeta[tournament.format];
  let content = "";
  if (state.tab === "players") {
    content = renderPlayers(payload);
  } else if (tournament.format === "single_elimination") {
    content = renderSingleBracket(payload);
  } else if (tournament.format === "double_elimination") {
    content = renderDoubleBracket(payload);
  } else {
    content = renderRoundRobin(payload);
  }

  app.innerHTML = `
    <header class="detail-topbar">
      <button class="back-button" data-action="home" aria-label="Назад">←</button>
      <div class="title">
        <h1>${escapeHtml(tournament.name)}</h1>
        <p>${format.label}</p>
      </div>
      <span class="badge ${status.className}">${status.label}</span>
    </header>
    ${
      tournament.champion_name
        ? `<section class="champion-banner">
            <span class="champion-icon">🏆</span>
            <span><small>ПОБЕДИТЕЛЬ ТУРНИРА</small><strong>${escapeHtml(tournament.champion_name)}</strong></span>
          </section>`
        : ""
    }
    ${
      tournament.can_manage && tournament.status === "registration"
        ? `<div class="action-row">
            <button class="primary-button" data-action="add-player">＋ Добавить игрока</button>
            ${
              tournament.player_count >= 2
                ? `<button class="secondary-button" data-action="start-tournament">▶ Сформировать</button>`
                : ""
            }
          </div>`
        : ""
    }
    <nav class="tabs" aria-label="Раздел турнира">
      <button class="tab ${state.tab === "bracket" ? "active" : ""}" data-tab="bracket">
        ${tournament.format === "round_robin" ? "Таблица и матчи" : "Сетка"}
      </button>
      <button class="tab ${state.tab === "players" ? "active" : ""}" data-tab="players">
        Игроки · ${tournament.player_count}
      </button>
    </nav>
    <section>${content}</section>
  `;
  updateTelegramBackButton(true);
  updateUrl(tournament.id);
}

function showCreateTournament() {
  openSheet(`
    <h2>Новый турнир</h2>
    <p class="sheet-description">Название и формат можно выбрать сейчас. Состав игроков добавите следующим шагом.</p>
    <form id="create-form">
      <div class="field">
        <label for="tournament-name">Название</label>
        <input id="tournament-name" name="name" maxlength="50" minlength="2" placeholder="Например, Кубок офиса" required />
      </div>
      <div class="format-options">
        ${Object.entries(formatMeta)
          .map(
            ([id, meta], index) => `
              <div class="format-option">
                <input type="radio" id="format-${id}" name="format" value="${id}" ${index === 0 ? "checked" : ""} />
                <label for="format-${id}">
                  <span class="format-icon">${meta.icon}</span>
                  <span><strong>${meta.label}</strong><small>${meta.description}</small></span>
                  <span class="radio-dot"></span>
                </label>
              </div>
            `,
          )
          .join("")}
      </div>
      <div class="sheet-actions">
        <button class="primary-button" type="submit">Создать турнир</button>
        <button class="secondary-button" type="button" data-action="close-sheet">Отмена</button>
      </div>
    </form>
  `);
}

async function showAddPlayer() {
  const tournamentId = state.current.tournament.id;
  try {
    const payload = await api(`/api/tournaments/${tournamentId}/available-players`);
    openSheet(`
      <h2>Добавить игрока</h2>
      <p class="sheet-description">Выберите зарегистрированного пользователя или добавьте гостя вручную.</p>
      ${
        payload.players.length
          ? `<div class="available-list">
              ${payload.players
                .map(
                  (player) => `
                    <button class="available-player" data-add-player="${player.id}">
                      ${escapeHtml(player.display_name)}
                      <span>＋</span>
                    </button>
                  `,
                )
                .join("")}
            </div>`
          : `<p class="sheet-description">Все зарегистрированные игроки уже добавлены.</p>`
      }
      <div class="divider">или гость</div>
      <form id="guest-form">
        <div class="field">
          <label for="guest-name">Имя игрока</label>
          <input id="guest-name" name="display_name" minlength="2" maxlength="50" placeholder="Введите имя" required />
        </div>
        <div class="sheet-actions">
          <button class="primary-button" type="submit">Добавить вручную</button>
          <button class="secondary-button" type="button" data-action="close-sheet">Закрыть</button>
        </div>
      </form>
    `);
  } catch (error) {
    notify(error.message, "error");
  }
}

function showWinner(matchId) {
  const match = state.current.matches.find((item) => item.id === Number(matchId));
  if (!match) return;
  openSheet(`
    <h2>Кто победил?</h2>
    <p class="sheet-description">После подтверждения сетка обновится автоматически.</p>
    <div class="winner-options">
      <button class="winner-option" data-winner="${match.player1_id}" data-match-id="${match.id}">
        ${escapeHtml(match.player1_name)} <span>🏆</span>
      </button>
      <button class="winner-option" data-winner="${match.player2_id}" data-match-id="${match.id}">
        ${escapeHtml(match.player2_name)} <span>🏆</span>
      </button>
    </div>
    <button class="secondary-button" data-action="close-sheet">Отмена</button>
  `);
}

function showWinnerConfirmation(matchId, winnerId) {
  const match = state.current.matches.find((item) => item.id === Number(matchId));
  if (!match) return;
  const winnerName =
    Number(winnerId) === Number(match.player1_id)
      ? match.player1_name
      : match.player2_name;
  openSheet(`
    <h2>Подтвердить результат?</h2>
    <p class="sheet-description">
      Победитель матча — <strong>${escapeHtml(winnerName)}</strong>.
      Изменить результат после сохранения нельзя.
    </p>
    <div class="sheet-actions">
      <button class="primary-button" data-record-winner="${winnerId}" data-match-id="${matchId}">
        Записать победу
      </button>
      <button class="secondary-button" data-action="close-sheet">Отмена</button>
    </div>
  `);
}

function showRemoveConfirmation(playerId) {
  const player = state.current.players.find((item) => item.id === Number(playerId));
  if (!player) return;
  openSheet(`
    <h2>Удалить игрока?</h2>
    <p class="sheet-description">
      <strong>${escapeHtml(player.display_name)}</strong> будет удалён из состава турнира.
    </p>
    <div class="sheet-actions">
      <button class="danger-button" data-confirm-remove="${playerId}">Удалить</button>
      <button class="secondary-button" data-action="close-sheet">Отмена</button>
    </div>
  `);
}

function showStartConfirmation() {
  const tournament = state.current.tournament;
  openSheet(`
    <h2>Сформировать турнир?</h2>
    <p class="sheet-description">
      Формат: <strong>${formatMeta[tournament.format].label}</strong>.
      После запуска состав игроков изменить нельзя.
    </p>
    <div class="sheet-actions">
      <button class="primary-button" data-action="confirm-start">Да, начать</button>
      <button class="secondary-button" data-action="close-sheet">Отмена</button>
    </div>
  `);
}

function showProfile() {
  openSheet(`
    <h2>Профиль игрока</h2>
    <p class="sheet-description">Это имя отображается во всех турнирных сетках.</p>
    <form id="profile-form">
      <div class="field">
        <label for="profile-name">Имя</label>
        <input id="profile-name" name="display_name" minlength="2" maxlength="50" value="${escapeHtml(state.user.display_name)}" required />
      </div>
      <div class="sheet-actions">
        <button class="primary-button" type="submit">Сохранить</button>
        <button class="secondary-button" type="button" data-action="close-sheet">Закрыть</button>
      </div>
    </form>
  `);
}

async function loadTournament(tournamentId) {
  try {
    state.current = await api(`/api/tournaments/${tournamentId}`);
    state.tab = "bracket";
    haptic();
    renderTournament();
  } catch (error) {
    notify(error.message, "error");
  }
}

async function reloadBootstrap() {
  const payload = await api("/api/bootstrap");
  state.user = payload.user;
  state.tournaments = payload.tournaments;
  state.maxPlayers = payload.max_players;
}

document.addEventListener("click", async (event) => {
  const tournamentButton = event.target.closest("[data-tournament]");
  if (tournamentButton) {
    await loadTournament(tournamentButton.dataset.tournament);
    return;
  }

  const tab = event.target.closest("[data-tab]");
  if (tab) {
    state.tab = tab.dataset.tab;
    haptic();
    renderTournament();
    return;
  }

  const match = event.target.closest("[data-match]");
  if (match) {
    haptic("medium");
    showWinner(match.dataset.match);
    return;
  }

  const removeButton = event.target.closest("[data-remove-player]");
  if (removeButton) {
    showRemoveConfirmation(removeButton.dataset.removePlayer);
    return;
  }

  const confirmRemoveButton = event.target.closest("[data-confirm-remove]");
  if (confirmRemoveButton) {
    const playerId = confirmRemoveButton.dataset.confirmRemove;
    try {
      state.current = await api(
        `/api/tournaments/${state.current.tournament.id}/players/${playerId}`,
        { method: "DELETE" },
      );
      closeSheet();
      haptic("medium");
      renderTournament();
      notify("Игрок удалён");
    } catch (error) {
      notify(error.message, "error");
    }
    return;
  }

  const addButton = event.target.closest("[data-add-player]");
  if (addButton) {
    try {
      state.current = await api(
        `/api/tournaments/${state.current.tournament.id}/players`,
        {
          method: "POST",
          body: JSON.stringify({ player_id: Number(addButton.dataset.addPlayer) }),
        },
      );
      closeSheet();
      haptic("medium");
      renderTournament();
      notify("Игрок добавлен");
    } catch (error) {
      notify(error.message, "error");
    }
    return;
  }

  const winnerButton = event.target.closest("[data-winner]");
  if (winnerButton) {
    haptic("medium");
    showWinnerConfirmation(
      winnerButton.dataset.matchId,
      winnerButton.dataset.winner,
    );
    return;
  }

  const recordWinnerButton = event.target.closest("[data-record-winner]");
  if (recordWinnerButton) {
    try {
      state.current = await api(
        `/api/matches/${recordWinnerButton.dataset.matchId}/winner`,
        {
          method: "POST",
          body: JSON.stringify({
            winner_id: Number(recordWinnerButton.dataset.recordWinner),
          }),
        },
      );
      closeSheet();
      haptic("heavy");
      renderTournament();
      notify("Результат записан");
    } catch (error) {
      notify(error.message, "error");
    }
    return;
  }

  const action = event.target.closest("[data-action]")?.dataset.action;
  if (!action) return;
  if (action === "close-sheet") closeSheet();
  if (action === "create-tournament") showCreateTournament();
  if (action === "add-player") await showAddPlayer();
  if (action === "start-tournament") showStartConfirmation();
  if (action === "profile") showProfile();
  if (action === "home") {
    await reloadBootstrap();
    renderHome();
  }
  if (action === "refresh") {
    try {
      await reloadBootstrap();
      renderHome();
      notify("Данные обновлены");
    } catch (error) {
      notify(error.message, "error");
    }
  }
  if (action === "confirm-start") {
    try {
      state.current = await api(
        `/api/tournaments/${state.current.tournament.id}/start`,
        { method: "POST", body: "{}" },
      );
      closeSheet();
      haptic("heavy");
      renderTournament();
      notify("Турнир начался");
    } catch (error) {
      notify(error.message, "error");
    }
  }
});

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const data = Object.fromEntries(new FormData(form).entries());

  try {
    if (form.id === "create-form") {
      state.current = await api("/api/tournaments", {
        method: "POST",
        body: JSON.stringify(data),
      });
      closeSheet();
      haptic("heavy");
      renderTournament();
      notify("Турнир создан");
    }
    if (form.id === "guest-form") {
      state.current = await api(
        `/api/tournaments/${state.current.tournament.id}/players`,
        { method: "POST", body: JSON.stringify(data) },
      );
      closeSheet();
      haptic("medium");
      renderTournament();
      notify("Игрок добавлен");
    }
    if (form.id === "profile-form") {
      const payload = await api("/api/profile", {
        method: "PATCH",
        body: JSON.stringify(data),
      });
      state.user = payload.user;
      closeSheet();
      haptic();
      if (state.current) renderTournament();
      else renderHome();
      notify("Имя обновлено");
    }
  } catch (error) {
    notify(error.message, "error");
  }
});

async function start() {
  try {
    if (tg) {
      tg.ready();
      tg.expand();
      tg.setHeaderColor?.("#07130f");
      tg.setBackgroundColor?.("#07130f");
      tg.setBottomBarColor?.("#07130f");
      tg.BackButton?.onClick(async () => {
        if (sheetRoot.innerHTML) closeSheet();
        else {
          await reloadBootstrap();
          renderHome();
        }
      });
    }
    await reloadBootstrap();
    const tournamentId = new URL(location.href).searchParams.get("tournament");
    if (tournamentId) await loadTournament(tournamentId);
    else renderHome();
  } catch (error) {
    app.innerHTML = `
      <section class="error-screen">
        <span class="error-icon">🔒</span>
        <h2>Не удалось открыть Mini App</h2>
        <p>${escapeHtml(error.message)} Попробуйте закрыть окно и открыть приложение из меню бота.</p>
      </section>
    `;
  }
}

start();
