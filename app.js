const SUPABASE_URL = 'https://msfcjiejmzduwycekavh.supabase.co';
const SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1zZmNqaWVqbXpkdXd5Y2VrYXZoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODE0MDkyMDYsImV4cCI6MjA5Njk4NTIwNn0.orhfYCoAvqfdNjuBoBAJ6YlGgFjEOOKnuNc8EPHcOrQ';
const sb = supabase.createClient(SUPABASE_URL, SUPABASE_KEY, {
  auth: {
    autoRefreshToken: false,
    persistSession: false,
    detectSessionInUrl: false
  }
});

function withTimeout(promise, ms = 8000, label = 'Operation') {
  return Promise.race([
    promise,
    new Promise((_, reject) => 
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms)
    )
  ]);
}

// ── 🔐 TELEGRAM ONLY GATEKEEPER AREA ──
const tg = window.Telegram?.WebApp;

// Strict Verification: If they don't have Telegram's signature web app data, kick them out immediately
if (!tg || !tg.initData || !tg.initDataUnsafe || !tg.initDataUnsafe.user) {
  document.body.innerHTML = `
      <div style="
        background: #0d0d0d; 
        color: #f5a623; 
        text-align: center; 
        padding: 50px 20px; 
        font-family: 'Bangers', cursive, sans-serif; 
        height: 100vh; 
        display: flex; 
        flex-direction: column; 
        justify-content: center; 
        align-items: center;
        letter-spacing: 1px;
      ">
        <h2 style="font-size: 36px; margin-bottom: 10px;">⚠️ ACCESS DENIED</h2>
        <p style="font-family: sans-serif; color: #f5f5f5; font-size: 14px; max-width: 300px; line-height: 1.5;">
          This arcade game is exclusive to the Telegram Mini App ecosystem. Please launch it through the official bot link.
        </p>
      </div>
    `;
  throw new Error("Security Intervention: Execution killed outside of authenticated Telegram WebApp environment.");
}

// If they pass the check, initialize their profile settings cleanly
let tgUser = {
  id: String(tg.initDataUnsafe.user.id),
  username: tg.initDataUnsafe.user.username || tg.initDataUnsafe.user.first_name || 'Player'
};

tg.expand();
tg.setHeaderColor('#0d0d0d');

let musicEnabled = true, sfxEnabled = true;

const SFX_IDS = ['food-sound', 'gold-sound', 'fud-sound', 'gameover-sound'];

function stopAllSfx() {
  SFX_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.pause(); el.currentTime = 0; }
  });
}

function playAudio(id, volume = 0.6) {
  if (!sfxEnabled) return;
  const el = document.getElementById(id);
  if (!el) return;
  // Stop any other SFX already playing before starting this one
  stopAllSfx();
  el.volume = volume;
  el.play().catch(() => { });
}

function startBgMusic() {
  const bg = document.getElementById('bg-music');
  if (!bg) return;
  bg.currentTime = 0;
  bg.volume = 0.4;
  if (musicEnabled) bg.play().catch(() => { });
}

function stopBgMusic() {
  const bg = document.getElementById('bg-music');
  if (bg) bg.pause();
}

const ITEM_TYPES = [
  {type: 'normal', emoji: '', label: 'MAMEINU', points: 10, lives: 0, weight: 61},
  {type: 'rare', emoji: '🐶', label: 'GOLDEN', points: 50, lives: 0, weight: 10},
  {type: 'heart', emoji: '❤️', label: 'LIFE+1', points: 0, lives: 1, weight: 2}, // Change 8 to 2
  {type: 'bad', emoji: '💣', label: 'FUD', points: 0, lives: -1, weight: 27}
];

let score = 0, lives = 3, items = [], animFrame = null;
let spawnTimeout = null, combo = 0, lastTime = null, gameRunning = false;
let currentSpeedBase = 1.8, nextMilestoneThreshold = 1000;
let playerHighScore = parseInt(localStorage.getItem('mameinu_highscore')) || 0;
let totalInvites = 0;
let claimedTasks = JSON.parse(localStorage.getItem('mameinu_claimed_tasks') || '[]');
let currentUser = null, onboardTasks = [], completedTasks = new Set();
let isPaused = false;

const arena = document.getElementById('arena');
const basket = document.getElementById('basket');
const scoreDisplay = document.getElementById('score-display');
const livesDisplay = document.getElementById('lives-display');
const endTitle = document.getElementById('end-title');
const rankMsg = document.getElementById('rank-msg');
const comboBanner = document.getElementById('combo-banner');
const dragHint = document.getElementById('drag-hint');
const navItems = document.querySelectorAll('.nav-item');
const progFill = document.getElementById('prog-fill');
const progLabel = document.getElementById('prog-label');
const tasksList = document.getElementById('tasks-list');
const btnUnlock = document.getElementById('btn-unlock');
const cdDisplay = document.getElementById('cd-display');
const settingsScreen = document.getElementById('settings-screen');
const mainMameinuImg = new Image();
mainMameinuImg.src = 'mame.png';

const screens = {
  loading: document.getElementById('loading-screen'),
  onboard: document.getElementById('onboard-screen'),
  countdown: document.getElementById('countdown-screen'),
  start: document.getElementById('start-screen'),
  game: document.getElementById('game-screen'),
  end: document.getElementById('end-screen'),
  leaderboard: document.getElementById('leaderboard-screen'),
  referral: document.getElementById('referral-screen'),
  settings: settingsScreen,
  notifications: document.getElementById('notifications-screen'),
  tasks: document.getElementById('tasks-screen'),
};

let toastTimer;
function toast(msg, dur = 2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), dur);
}

function showScreen(name) {
  Object.entries(screens).forEach(([k, el]) => {
    if (el) el.classList.toggle('hidden', k !== name);
  });
}

// ── CHECK GAME PAUSE STATE (from admin panel) ──
async function checkGamePause() {
  try {
    const { data } = await sb
      .from('game_config')
      .select('value')
      .eq('key', 'paused')
      .maybeSingle();
    
    if (data?.value === 'true') {
      isPaused = true;
      gameRunning = false;
      // Show maintenance message on start screen
      const msg = document.getElementById('maintenance-msg');
      if (msg) msg.style.display = 'block';
      console.log('⏸ Game is paused by admin');
    }
  } catch (e) {
    console.log('⚠️ Could not check pause state (game_config table may not exist yet)');
  }
}

async function init() {
  showScreen('loading');
  try {
    // 1. Check if game is paused by admin safely (with a 5-second max timeout)
    await withTimeout(checkGamePause(), 5000, 'checkGamePause');

    const refBox = document.getElementById('ref-string');
    if (refBox) refBox.textContent = `https://t.me/mameinubot/app?startapp=${tgUser.id}`;

    // 2. Fetch or create user record safely (wrapped with timeout protection)
    const { data: user, error: fetchErr } = await withTimeout(
      sb.from('users').select('*').eq('telegram_id', tgUser.id).maybeSingle(),
      5000,
      'fetchUser'
    );

    if (fetchErr) throw fetchErr;
    let activeUser = user;

    if (!activeUser) {
      const { data: newUser, error: insertErr } = await withTimeout(
        sb.from('users').insert({
          telegram_id: tgUser.id,
          username: tgUser.username,
          tasks_completed: [],
          is_verified: false,
        }).select().single(),
        5000,
        'createUser'
      );

      if (insertErr) throw insertErr;
      activeUser = newUser;
    }

    if (!activeUser) throw new Error('User record unavailable');
    currentUser = activeUser;

    if (Array.isArray(currentUser.tasks_completed)) {
      currentUser.tasks_completed.forEach(t => completedTasks.add(String(t)));
    }

    // 3. Count total referral signups
    const { count, error: countErr } = await withTimeout(
      sb.from('users').select('*', { count: 'exact', head: true }).eq('referred_by', tgUser.id),
      5000,
      'countReferrals'
    );
    if (countErr) console.warn('Referral count failed:', countErr);
    totalInvites = count || 0;

    // 4. Fetch your mandatory onboarding tasks
    const { data: taskData, error: taskErr } = await withTimeout(
      sb.from('tasks').select('*').eq('is_active', true).order('sort_order'),
      5000,
      'fetchTasks'
    );
    if (taskErr) throw taskErr;
    onboardTasks = taskData || [];

    // 5. Update user panels
    updateSettingsPanel();

    // 6. Check broadcasts inside an isolated safety block so a failure here won't halt the app
    try {
      await withTimeout(checkSystemBroadcasts(), 4000, 'checkBroadcasts');
    } catch (broadcastErr) {
      console.warn('⚠️ Omitted system announcements fetch:', broadcastErr);
    }

    // 7. Security & Router Guard Gatekeeping
    if (isPaused) {
      showScreen('start');
      return; // Stop execution right here if maintenance mode is true
    }

    if (currentUser.is_verified) {
      showScreen('start');
    } else {
      renderOnboarding();
      showScreen('onboard');
    }

  } catch (e) {
    console.error('Initialization error:', e);
    
    // Custom clean error fallback layout featuring the responsive RETRY button
    document.getElementById('loading-screen').innerHTML = `
      <div style="color:#f5a623; text-align:center; padding:40px 20px; font-family:sans-serif; height:100%; display:flex; flex-direction:column; justify-content:center; align-items:center;">
        <h2 style="font-family:'Bangers', sans-serif; font-size:28px; letter-spacing:1px; margin-bottom:10px;">⚠️ CONNECTION ISSUE</h2>
        <p style="color:#f5f5f5; font-size:14px; margin:0 0 20px 0; line-height:1.4;">
          Could not connect to the game server.<br>Please check your internet and try again.
        </p>
        <button onclick="location.reload()" style="padding:10px 24px; background:var(--gold, #f5a623); color:#000; border:none; border-radius:20px; font-family:'Bangers', sans-serif; font-size:18px; cursor:pointer; font-weight:bold; box-shadow: 0 4px 6px rgba(0,0,0,0.2);">
          RETRY
        </button>
      </div>
    `;
  }
}

function updateSettingsPanel() {
  const uEl = document.getElementById('settings-username');
  const hEl = document.getElementById('settings-highscore');
  const wEl = document.getElementById('settings-wallet-display');
  if (uEl) uEl.textContent = tgUser.username;
  if (hEl) hEl.textContent = playerHighScore.toLocaleString() + ' pts';
  if (wEl && currentUser?.wallet_address) {
    const w = currentUser.wallet_address;
    wEl.textContent = w.slice(0, 6) + '…' + w.slice(-4);
  }
}

function getTaskIcon(title) {
  const t = (title || '').toLowerCase();
  if (t.includes('telegram')) return '✈️';
  if (t.includes('x') || t.includes('twitter') || t.includes('follow')) return '🐦';
  if (t.includes('retweet') || t.includes('share')) return '🔁';
  if (t.includes('discord')) return '💬';
  if (t.includes('wallet')) return '💼';
  return '🎯';
}

// ── 🛠️ UPGRADED DYNAMIC ONBOARDING SYSTEM ──

function renderOnboarding() {
  if (!tasksList) return;
  tasksList.innerHTML = '';
  
  if (onboardTasks.length === 0) {
    tasksList.innerHTML = '<div class="lb-loading">No required entry tasks. Click play below! 🐾</div>';
    updateProgress();
    return;
  }

  onboardTasks.forEach(task => {
    // Check if task ID exists in the user's completed set
    const done = completedTasks.has(String(task.id));
    const card = document.createElement('div');
    card.className = `task-card${done ? ' done' : ''}${task.type === 'wallet' ? ' wallet-card' : ''}`;

    if (task.type === 'wallet') {
      card.innerHTML = `
        <div class="wallet-row-top">
          <div class="task-icon">${getTaskIcon(task.title)}</div>
          <div class="task-info">
            <h3>${escapeHtml(task.title)}</h3>
            <p>${escapeHtml(task.description || 'Enter BEP-20 address')}</p>
            ${task.points > 0 ? `<p style="color:#ffc84a;font-weight:700;font-size:12px;margin-top:3px;">🎁 +${task.points} pts on completion</p>` : ''}
          </div>
          <div class="task-check">${done ? '✓' : ''}</div>
        </div>
        ${!done ? `
        <div class="wallet-row" style="display: flex; gap: 8px; margin-top: 10px; width: 100%;">
          <input class="wallet-input" placeholder="0x…" id="wi-${task.id}" maxlength="42" style="flex: 1; padding: 8px; border-radius: 6px; border: 1px solid var(--border); background: #000; color: #fff;"/>
          <button class="wallet-submit" onclick="submitWallet(${task.id})" style="background: var(--gold); color: #000; border: none; padding: 8px 16px; font-weight: bold; border-radius: 6px; cursor: pointer;">SAVE</button>
        </div>` : ''}`;
    } else {
      card.innerHTML = `
        <div class="task-icon">${getTaskIcon(task.title)}</div>
        <div class="task-info">
          <h3>${escapeHtml(task.title)}</h3>
          <p>${escapeHtml(task.description || 'Click GO to participate')}</p>
          ${task.points > 0 ? `<p style="color:#ffc84a;font-weight:700;font-size:12px;margin-top:3px;">🎁 +${task.points} pts on completion</p>` : ''}
        </div>
        ${done ? `<div class="task-check">✓</div>` : `<button class="task-btn" data-task-id="${task.id}" data-task-link="${escapeHtml(task.link || '')}" data-task-points="${task.points || 0}">GO →</button>`}`;
    }

    tasksList.appendChild(card);
  });

  // Attach proper listeners for the standard redirects
  tasksList.querySelectorAll('.task-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = parseInt(btn.getAttribute('data-task-id'));
      const link = btn.getAttribute('data-task-link');
      const points = parseInt(btn.getAttribute('data-task-points')) || 0;
      openTask(id, link, points);
    });
  });

  updateProgress();
}

function updateProgress() {
  const total = onboardTasks.length;
  const done = onboardTasks.filter(t => completedTasks.has(String(t.id))).length;
  
  // Update progress slider bar visually
  if (progFill) progFill.style.width = (total > 0 ? (done / total) * 100 : 0) + '%';
  if (progLabel) progLabel.textContent = `${done} / ${total} tasks completed`;
  
  const allDone = done >= total;
  
  if (btnUnlock) {
    btnUnlock.disabled = !allDone;
    if (total === 0) {
      btnUnlock.disabled = false;
      btnUnlock.textContent = '官 PLAY NOW!';
    } else {
      btnUnlock.textContent = allDone ? '🎮 PLAY NOW!' : `COMPLETE ALL TASKS (${done}/${total})`;
    }
  }
}

async function openTask(taskId, link, points) {
  if (link && link !== 'null' && link !== '') {
    // Use Telegram WebApp browser window if running inside mobile app
    if (window.Telegram?.WebApp) {
      window.Telegram.WebApp.openLink(link);
    } else {
      window.open(link, '_blank');
    }
  }
  
  // Give them 2 seconds to view the link, then mark it complete automatically
  setTimeout(async () => {
    await markDone(taskId, points);
  }, 2000);
}

// ── CONNECT THE "PLAY NOW" BUTTON TRIGGER ──
if (btnUnlock) {
  btnUnlock.addEventListener('click', async () => {
    try {
      // 1. Permanently record validation profile state into Supabase
      const { error } = await sb
        .from('users')
        .update({ is_verified: true })
        .eq('telegram_id', tgUser.id);
        
      if (error) throw error;
      
      if (currentUser) currentUser.is_verified = true;
      
      // 2. Play the countdown numbers interface and route them into the main room!
      startCountdown();
    } catch (e) {
      console.error(e);
      toast('❌ Verification save error. Please try again.');
    }
  });
}

async function submitWallet(taskId) {
  const input = document.getElementById('wi-' + taskId);
  const addr = input?.value?.trim() || '';
  if (!/^0x[0-9a-fA-F]{40}$/.test(addr)) {
    toast('⚠️ Enter a valid 42-character BSC address (0x…)');
    return;
  }
  const {error} = await sb.from('users').update({wallet_address: addr}).eq('telegram_id', tgUser.id);
  if (error) {toast('❌ Failed to save wallet'); return;}
  if (currentUser) currentUser.wallet_address = addr;
  // Find the points for this task
  const task = onboardTasks.find(t => String(t.id) === String(taskId));
  await markDone(taskId, task?.points || 0);
  updateSettingsPanel();
}

window.submitWallet = submitWallet;

async function markDone(taskId, points) {
  completedTasks.add(String(taskId));
  const {error} = await sb
    .from('users')
    .update({tasks_completed: Array.from(completedTasks)})
    .eq('telegram_id', tgUser.id);
  if (error) console.error('markDone error:', error);

  // Award points if the task has a reward
  if (points && points > 0) {
    await sb.from('scores').insert({
      telegram_id: tgUser.id,
      username: tgUser.username,
      score: points,
      source: 'task_reward',
    });
    toast(`✅ Task done! +${points} pts 🎉`);
  } else {
    toast('✅ Task completed!');
  }

  renderOnboarding();
}

function startCountdown() {
  showScreen('countdown');
  const steps = ['3', '2', '1', 'GO!'];
  let i = 0;
  function next() {
    if (i >= steps.length) {showScreen('start'); return;}
    cdDisplay.className = steps[i] === 'GO!' ? 'cd-go' : 'cd-num';
    cdDisplay.textContent = steps[i];
    i++;
    setTimeout(next, steps[i - 1] === 'GO!' ? 700 : 900);
  }
  next();
}

function showView(targetName) {
  if (targetName === 'game') {
    if (gameRunning) {
      showScreen('game');
      setBasketX(basketX);
      if (!animFrame && isPaused === false) {
        lastTime = performance.now();
        animFrame = requestAnimationFrame(gameLoop);
      }
    } else {
      showScreen('start');
    }
  } else if (targetName === 'leaderboard') {
    if (gameRunning) {
      isPaused = true;
      if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
      stopBgMusic();
    }
    loadLeaderboard();
    showScreen('leaderboard');
  } else if (targetName === 'referral') {
    if (gameRunning) {
      isPaused = true;
      if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
      stopBgMusic();
    }
    renderRefTasks();
    showScreen('referral');
  } else if (targetName === 'settings') {
    updateSettingsPanel();
    if (gameRunning) {
      settingsScreen.classList.add('only-audio');
    } else {
      settingsScreen.classList.remove('only-audio');
    }
    showScreen('settings');
  } else if (targetName === 'notifications') {
    // 📢 Full notifications screen — loads all broadcasts live from DB
    if (gameRunning) {
      isPaused = true;
      if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
      stopBgMusic();
    }
    loadNotificationsScreen();
    showScreen('notifications');
  } else if (targetName === 'tasks') {
    // 📋 Standalone tasks screen — always fetches fresh from DB
    if (gameRunning) {
      isPaused = true;
      if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
      stopBgMusic();
    }
    loadTasksScreen();
    showScreen('tasks');
  } else if (targetName === 'start') {
    // FIX 2: Added missing 'start' case — back-to-menu-btn called showView('start')
    // but there was no handler, so the button did nothing when not in a game.
    showScreen('start');
  }
}

// FIX 3: Back-to-menu button now also restarts spawnTimeout on game resume
// so items don't freeze after returning from leaderboard/referral/settings.
document.querySelectorAll('.back-to-menu-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (gameRunning) {
      isPaused = false;
      if (musicEnabled) startBgMusic();
      // Restart the spawn loop that was cancelled when we paused
      clearTimeout(spawnTimeout);
      spawnTimeout = setTimeout(spawnItem, 300);
      showView('game');
    } else {
      isPaused = false;
      showView('start');
    }
  });
});

navItems.forEach(btn =>
  btn.addEventListener('click', () => showView(btn.getAttribute('data-target')))
);

/* ── MAIN MENU ICON TRIGGERS ── */
document.getElementById('menu-lb-btn')?.addEventListener('click', () => {
  showView('leaderboard');
});
document.getElementById('menu-ref-btn')?.addEventListener('click', () => {
  showView('referral');
});
document.getElementById('menu-set-btn')?.addEventListener('click', () => {
  showView('settings');
});
document.getElementById('menu-notif-btn')?.addEventListener('click', () => {
  showView('notifications');
});
document.getElementById('menu-tasks-btn')?.addEventListener('click', () => {
  showView('tasks');
});

// FIX 1: Removed the `if (gameRunning)` guard so the gear icon always opens
// settings — previously it did nothing when clicked from the start screen.
document.getElementById('game-settings-btn')?.addEventListener('click', () => {
  if (gameRunning) {
    isPaused = true;
    if (animFrame) {
      cancelAnimationFrame(animFrame);
      animFrame = null;
    }
    stopBgMusic();
  }
  showView('settings');
});
async function loadLeaderboard() {
  const container = document.getElementById('leaderboard-list');
  if (!container) return;
  container.innerHTML = '<div class="lb-loading">Loading Rankings...</div>';
  
  try {
    // 1. Query the view using the correct column name seen in image_fa9ebb.png (best_score)
    let { data, error } = await sb
      .from('leaderboard')
      .select('username, telegram_id, best_score')
      .order('best_score', { ascending: false })
      .limit(50);

    // 2. Fallback: If the view is completely empty (as shown in the dashboard), pull from raw scores
    if (error || !data || data.length === 0) {
      console.warn("Leaderboard view is empty or restricted. Falling back to raw scores...");
      
      const { data: rawData, error: fallbackError } = await sb
        .from('scores')
        .select('username, score, telegram_id')
        .order('score', { ascending: false })
        .limit(200);

      if (fallbackError) throw fallbackError;

      // Deduplicate the raw game plays so each player only shows up once with their peak score
      const seen = new Set();
      data = [];
      for (const row of rawData || []) {
        const uid = String(row.telegram_id);
        if (!seen.has(uid)) {
          seen.add(uid);
          data.push({
            username: row.username,
            telegram_id: row.telegram_id,
            best_score: row.score // Map 'score' to 'best_score' for layout uniformity
          });
        }
        if (data.length >= 50) break;
      }
    }

    // 3. Render the UI
    if (!data || !data.length) {
      container.innerHTML = '<div class="lb-loading">No scores yet! Be the first! 🐾</div>';
      return;
    }

    container.innerHTML = '';
    const medals = ['🥇', '🥈', '🥉'];
    
    data.forEach((row, i) => {
      const cls = i === 0 ? 'podium-1' : i === 1 ? 'podium-2' : i === 2 ? 'podium-3' : '';
      const isYou = String(row.telegram_id) === String(tgUser.id);
      
      // Keep local high score updated if the DB record is higher
      if (isYou && row.best_score > playerHighScore) {
        playerHighScore = row.best_score;
        localStorage.setItem('mameinu_highscore', playerHighScore);
      }

      const div = document.createElement('div');
      div.className = `leaderboard-row ${cls}`;
      div.innerHTML = `
        <div class="rank-badge">${medals[i] || '#' + (i + 1)}</div>
        <div class="player-name">${escapeHtml(row.username || 'Anonymous')}${isYou ? ' <span>YOU</span>' : ''}</div>
        <div class="player-score">${Number(row.best_score).toLocaleString()}</div>`;
      container.appendChild(div);
    });
    
    updateSettingsPanel();
    
  } catch (e) {
    console.error('Leaderboard error:', e);
    container.innerHTML = '<div class="lb-loading" style="color:var(--red);">⚠️ Failed to load rankings</div>';
  }
}



function renderRefTasks() {
  document.querySelectorAll('.btn-claim').forEach(btn => {
    const req = parseInt(btn.getAttribute('data-task'));
    if (claimedTasks.includes(req)) {
      btn.className = 'btn-claim completed';
      btn.textContent = 'CLAIMED';
      btn.disabled = true;
    } else if (totalInvites >= req) {
      btn.className = 'btn-claim ready';
      btn.textContent = 'CLAIM';
      btn.disabled = false;
    } else {
      btn.className = 'btn-claim';
      btn.textContent = `PROGRESS: ${totalInvites}/${req}`;
      btn.disabled = true;
    }
  });
}

document.querySelectorAll('.btn-claim').forEach(btn => {
  btn.addEventListener('click', async () => {
    const req = parseInt(btn.getAttribute('data-task'));
    if (totalInvites < req || claimedTasks.includes(req)) return;

    const pts = req === 1 ? 50 : req === 5 ? 250 : 500;
    await sb.from('scores').insert({
      telegram_id: tgUser.id,
      username: tgUser.username,
      score: pts,
      source: 'referral_reward',
    });

    // FIX 4: Removed playerHighScore += pts and its localStorage write.
    // Referral bonuses are not game scores and should not inflate the
    // personal best displayed in settings or used for high-score tracking.
    claimedTasks.push(req);
    localStorage.setItem('mameinu_claimed_tasks', JSON.stringify(claimedTasks));
    await sb.from('users')
      .update({claimed_referral_tasks: claimedTasks})
      .eq('telegram_id', tgUser.id);

    renderRefTasks();
    updateSettingsPanel();
    toast(`+${pts} points claimed! 🎉`);
  });
});

document.getElementById('btn-copy-ref')?.addEventListener('click', () => {
  const url = document.getElementById('ref-string')?.textContent || '';
  navigator.clipboard.writeText(url).then(() => {
    const btn = document.getElementById('btn-copy-ref');
    btn.textContent = 'COPIED!'; btn.style.background = '#3ecf6e';
    toast('Invite link copied!');
    setTimeout(() => {btn.textContent = 'COPY'; btn.style.background = '';}, 2000);
  }).catch(() => toast('❌ Copy failed'));
});

const toggleMusic = document.getElementById('toggle-music');
const toggleSfx = document.getElementById('toggle-sfx');

toggleMusic?.addEventListener('change', () => {
  musicEnabled = toggleMusic.checked;
  if (!musicEnabled) stopBgMusic();
  else if (gameRunning) startBgMusic();
});

toggleSfx?.addEventListener('change', () => {
  sfxEnabled = toggleSfx.checked;
});

document.getElementById('settings-wallet-save')?.addEventListener('click', async () => {
  const input = document.getElementById('settings-wallet-input');
  const addr = input?.value?.trim() || '';
  const errEl = document.getElementById('settings-wallet-err');
  if (!/^0x[0-9a-fA-F]{40}$/.test(addr)) {
    if (errEl) {errEl.textContent = '⚠️ Invalid BSC address'; errEl.style.display = 'block';}
    return;
  }
  if (errEl) errEl.style.display = 'none';
  const {error} = await sb.from('users').update({wallet_address: addr}).eq('telegram_id', tgUser.id);
  if (error) {toast('❌ Failed to save wallet'); return;}
  if (currentUser) currentUser.wallet_address = addr;
  if (input) input.value = '';
  updateSettingsPanel();
  toast('✅ Wallet saved!');
});

document.getElementById('btn-share')?.addEventListener('click', () => {
  const text = `🐕 I scored ${score} points in Catch the MAMEINU! Can you beat me? 🔥\n\nPlay here 👉 https://t.me/mameinu_bot/app?startapp=${tgUser.id}`;
  if (navigator.share) {
    navigator.share({title: 'Catch the MAMEINU', text}).catch(() => { });
  } else {
    navigator.clipboard.writeText(text).then(() => {
      const btn = document.getElementById('btn-share');
      if (btn) {btn.textContent = '✅ COPIED!'; setTimeout(() => {btn.textContent = '📤 SHARE SCORE';}, 2000);}
    }).catch(() => toast('📤 Copy failed — share manually!'));
  }
});

document.getElementById('btn-retry')?.addEventListener('click', startGame);
document.getElementById('btn-start')?.addEventListener('click', startGame);
document.getElementById('btn-end-menu')?.addEventListener('click', () => {
  showScreen('start');
});

let basketX = 0;
const BASKET_HALF = 45;

function setBasketX(x) {
  const w = arena.offsetWidth;
  basketX = Math.max(BASKET_HALF, Math.min(w - BASKET_HALF, x));
  basket.style.left = (basketX - BASKET_HALF) + 'px';
}

// 🎮 SMOOTHED MOVEMENT CONTROL
arena.addEventListener('mousemove', e => {
  if (!gameRunning) return;
  const targetX = e.clientX - arena.getBoundingClientRect().left;
  // Blend current position with new target. 0.6 = smooth. Lower is slower/less sensitive.
  const smoothX = basketX + (targetX - basketX) * 0.6;
  setBasketX(smoothX);
});

arena.addEventListener('touchmove', e => {
  if (!gameRunning) return;
  e.preventDefault();
  const targetX = e.touches[0].clientX - arena.getBoundingClientRect().left;
  const smoothX = basketX + (targetX - basketX) * 0.6;
  setBasketX(smoothX);
}, {passive: false});

arena.addEventListener('touchstart', e => {
  if (!gameRunning) return;
  e.preventDefault();
  const targetX = e.touches[0].clientX - arena.getBoundingClientRect().left;
  const smoothX = basketX + (targetX - basketX) * 0.6;
  setBasketX(smoothX);
}, {passive: false});

const keysDown = {};
document.addEventListener('keydown', e => {keysDown[e.key] = true;});
document.addEventListener('keyup', e => {keysDown[e.key] = false;});

function pickType() {
  const total = ITEM_TYPES.reduce((s, t) => s + t.weight, 0);
  let r = Math.random() * total;
  for (let i = 0; i < ITEM_TYPES.length; i++) {
    if (r < ITEM_TYPES[i].weight) {
      return ITEM_TYPES[i];
    }
    r -= ITEM_TYPES[i].weight;
  }
  return ITEM_TYPES[0];
}

function spawnItem() {
  if (!gameRunning) return;

  if (isPaused) {
    spawnTimeout = setTimeout(spawnItem, 500);
    return;
  }

  const arenaW = arena.offsetWidth;
  const def = pickType();
  const el = document.createElement('div');
  el.className = `item ${def.type}`;
  
  // ✅ FIXED LAYER: Check if the definition uses an image asset instead of a text emoji
  if (def.isImage) {
    el.innerHTML = `<div class="coin-design" style="background: url('${def.emoji}') center/contain no-repeat; background-color: transparent !important; border: none !important; box-shadow: none !important;"></div>`;
  } else {
    el.innerHTML = `<div class="coin-design">${def.emoji}</div>`;
  }

  const x = 25 + Math.random() * (arenaW - 50);
  const speed = currentSpeedBase + Math.random() * 0.4;
  el.style.left = x + 'px';
  el.style.top = '-70px';
  arena.appendChild(el);
  items.push({el, x, y: -70, speed, def});
  const delay = Math.max(350, 1100 - currentSpeedBase * 100) + Math.random() * 400;
  spawnTimeout = setTimeout(spawnItem, delay);
}

function gameLoop(ts) {
  if (!gameRunning) return;

  if (isPaused) {
    lastTime = ts;
    animFrame = requestAnimationFrame(gameLoop);
    return;
  }

  if (keysDown['ArrowLeft'] || keysDown['a']) setBasketX(basketX - 8);
  if (keysDown['ArrowRight'] || keysDown['d']) setBasketX(basketX + 8);

  const dt = lastTime ? Math.min((ts - lastTime) / 16.67, 3) : 1;
  lastTime = ts;
  const arenaH = arena.offsetHeight;
  const catchZoneTop = arenaH - 72;
  const catchZoneBot = arenaH - 12;

  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    item.y += item.speed * dt;
    item.el.style.top = item.y + 'px';

    if (item.y + 44 >= catchZoneTop && item.y <= catchZoneBot && Math.abs(item.x - basketX) < BASKET_HALF + 16) {
      handleCatch(item, i);
      continue;
    }

    if (item.y > arenaH + 20) {
      item.el.remove();
      items.splice(i, 1);
      if (item.def.type !== 'bad') combo = 0;
    }
  }
  animFrame = requestAnimationFrame(gameLoop);
}

function handleCatch(item, idx) {
  const {def, x, y} = item;
  item.el.remove();
  items.splice(idx, 1);

  if (def.type === 'bad') {
    playAudio('fud-sound', 0.6);
  } else if (def.type === 'rare') {
    playAudio('gold-sound', 0.8);
  } else {
    playAudio('food-sound', 0.55);
  }

  if (def.lives !== 0) {
    lives = Math.min(5, lives + def.lives);
    if (def.lives < 0) {combo = 0; flashArena('#e84040');}
    else {flashArena('#3ecf6e');}
    renderLives();
    if (lives <= 0) {endGame(); return;}
  }

  if (def.points > 0) {
    combo++;
    const multiplier = Math.min(combo, 5);
    const earned = def.points * multiplier;
    score += earned;
    scoreDisplay.textContent = score;
    showPop(x, y, `+${earned}${multiplier > 1 ? ' x' + multiplier : ''}`, def.type === 'rare' ? '#ffc84a' : '#ffffff');
    spawnParticles(x, y + 20, def.type === 'rare' ? '#ffc84a' : '#f5a623');
    if (combo >= 3) showCombo(combo);
    if (score >= nextMilestoneThreshold) triggerSpeedUp();
  } else if (def.lives > 0) {
    showPop(x, y, '+❤️', '#3ecf6e');
    spawnParticles(x, y + 20, '#3ecf6e');
    combo++;
  }
}

function triggerSpeedUp() {
  while (score >= nextMilestoneThreshold) {
    nextMilestoneThreshold += 1000;
    currentSpeedBase += 0.65;
  }
  comboBanner.textContent = 'SPEED UP! ⚡';
  comboBanner.style.color = 'var(--red)';
  comboBanner.style.opacity = '1';
  flashArena('rgba(255,200,74,0.4)');
  setTimeout(() => {comboBanner.style.opacity = '0'; comboBanner.style.color = 'var(--gold)';}, 1200);
}

function flashArena(color) {
  arena.style.transition = 'background 0.05s';
  arena.style.background = color + '22';
  setTimeout(() => {arena.style.background = '';}, 200);
}

function showPop(x, y, text, color) {
  const el = document.createElement('div');
  el.className = 'catch-pop';
  el.textContent = text;
  el.style.left = (x - 20) + 'px';
  el.style.top = (y + 10) + 'px';
  el.style.color = color;
  arena.appendChild(el);
  setTimeout(() => el.remove(), 800);
}

function spawnParticles(x, y, color) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < 7; i++) {
    const p = document.createElement('div');
    p.className = 'particle';
    p.style.background = color;
    p.style.left = x + 'px';
    p.style.top = y + 'px';
    const angle = (Math.PI * 2 * i) / 7;
    const dist = 30 + Math.random() * 25;
    p.style.setProperty('--dx', Math.cos(angle) * dist + 'px');
    p.style.setProperty('--dy', Math.sin(angle) * dist + 'px');
    frag.appendChild(p);
    setTimeout(() => p.remove(), 600);
  }
  arena.appendChild(frag);
}

let comboHideTimeout;
function showCombo(n) {
  const msgs = {3: 'TRIPLE! 🔥', 4: 'QUAD! 🔥🔥', 5: 'ON FIRE! 🔥🔥🔥'};
  comboBanner.textContent = msgs[Math.min(n, 5)] || `x${n} COMBO!`;
  comboBanner.style.opacity = '1';
  clearTimeout(comboHideTimeout);
  comboHideTimeout = setTimeout(() => {comboBanner.style.opacity = '0';}, 900);
}

// FIX 5: renderLives now correctly handles lives above 3 (possible via heart items,
// capped at 5). Previously Math.max(0, 3 - lives) would show 0 black hearts for
// any lives > 3, but the display now clamps to MAX_LIVES for red hearts display.
function renderLives() {
  const MAX_LIVES = 5;
  const display = Math.min(lives, MAX_LIVES);
  livesDisplay.textContent = '❤️'.repeat(Math.max(0, display)) + '🖤'.repeat(Math.max(0, 3 - display));
}

async function startGame() {
  // 1. Check live pause status from Supabase game_config table
  try {
    const { data, error } = await sb
      .from('game_config')
      .select('value')
      .eq('key', 'paused')
      .maybeSingle();

    // If the database says true, block the game right here
    if (data && data.value === 'true') {
      toast('⏸ Game is currently paused for maintenance. Please try again later.', 'err');
      return; 
    }
  } catch (err) {
    console.error("Error checking game pause state:", err);
  }

  // 2. Your existing game startup setup continues exactly as it was:
  score = 0; lives = 3; combo = 0; items = []; lastTime = null;
  gameRunning = true; isPaused = false; currentSpeedBase = 1.8; nextMilestoneThreshold = 1000;
  arena.querySelectorAll('.item,.catch-pop,.particle').forEach(e => e.remove());
  scoreDisplay.textContent = '0';
  renderLives();
  dragHint.style.opacity = '1';
  setBasketX(arena.offsetWidth / 2);
  showScreen('game');
  startBgMusic();
  setTimeout(() => {spawnItem(); animFrame = requestAnimationFrame(gameLoop);}, 300);
  setTimeout(() => {dragHint.style.opacity = '0';}, 3000);
}

function killGameEngine() {
  gameRunning = false;
  clearTimeout(spawnTimeout);
  cancelAnimationFrame(animFrame);
  animFrame = null;
}

async function endGame() {
  killGameEngine();
  stopBgMusic();
  playAudio('gameover-sound', 0.65);

  document.getElementById('score-final').textContent = score;

  if (score > playerHighScore) {
    playerHighScore = score;
    localStorage.setItem('mameinu_highscore', playerHighScore);
    updateSettingsPanel();
  }

  if (score >= 1500) {endTitle.textContent = '🏆 LEGEND!'; rankMsg.textContent = 'Ultimate Crypto Master!';}
  else if (score >= 800) {endTitle.textContent = '🔥 ON FIRE!'; rankMsg.textContent = 'Incredible reflexes! The Mameinu approves.';}
  else if (score >= 400) {endTitle.textContent = '👏 NICE RUN!'; rankMsg.textContent = 'Solid score! Can you beat your record?';}
  else if (score >= 100) {endTitle.textContent = '🐾 NOT BAD!'; rankMsg.textContent = 'Keep practicing, the Mameinu believes in you.';}
  else {endTitle.textContent = '😅 KEEP GOING!'; rankMsg.textContent = 'The FUD got you this time. Try again!';}

  showScreen('end');

  try {
    const {error} = await sb.from('scores').insert({
      telegram_id: tgUser.id,
      username: tgUser.username,
      score,
    });
    if (error) console.error('Score save error:', error);
  } catch (e) {console.error('Score save exception:', e);}
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── 🔔 NOTIFICATIONS SCREEN — pulls all broadcasts live from Supabase ──
async function loadNotificationsScreen() {
  const container = document.getElementById('notifications-list');
  if (!container) return;
  container.innerHTML = '<div class="lb-loading">Loading announcements...</div>';

  try {
    const { data, error } = await sb
      .from('broadcasts')
      .select('id, title, body, type, target, created_at')
      .order('created_at', { ascending: false })
      .limit(30);

    if (error) throw error;

    if (!data || data.length === 0) {
      container.innerHTML = '<div class="lb-loading">No announcements yet. Check back soon! 📢</div>';
      return;
    }

    // Type → colour mapping matching admin panel badges
    const typeConfig = {
      info:    { emoji: 'ℹ️',  color: '#4a9eff', bg: 'rgba(74,158,255,0.10)',  label: 'INFO' },
      update:  { emoji: '🚀', color: '#3ecf6e', bg: 'rgba(62,207,110,0.10)',  label: 'UPDATE' },
      warning: { emoji: '⚠️', color: '#f5a623', bg: 'rgba(245,166,35,0.10)',  label: 'WARNING' },
      reward:  { emoji: '🎁', color: '#ffc84a', bg: 'rgba(255,200,74,0.10)',  label: 'REWARD' },
    };

    container.innerHTML = '';
    data.forEach(b => {
      const cfg = typeConfig[b.type] || { emoji: '📢', color: '#f5a623', bg: 'rgba(245,166,35,0.10)', label: (b.type || 'NOTICE').toUpperCase() };
      const dateStr = b.created_at ? new Date(b.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '';
      const card = document.createElement('div');
      card.style.cssText = `
        background: ${cfg.bg};
        border: 1px solid ${cfg.color}44;
        border-radius: 14px;
        padding: 14px 16px;
        margin-bottom: 12px;
        position: relative;
      `;
      card.innerHTML = `
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
          <span style="font-size:20px;line-height:1;">${cfg.emoji}</span>
          <div style="flex:1;">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
              <strong style="font-family:'Bangers',cursive;font-size:16px;letter-spacing:0.5px;color:#f5f5f5;">${escapeHtml(b.title)}</strong>
              <span style="background:${cfg.color}22;color:${cfg.color};font-size:10px;font-weight:700;padding:2px 7px;border-radius:20px;letter-spacing:1px;">${cfg.label}</span>
            </div>
            <div style="font-size:11px;color:rgba(255,255,255,0.38);margin-top:2px;">${escapeHtml(dateStr)}</div>
          </div>
        </div>
        <p style="font-size:13px;color:rgba(245,245,245,0.85);line-height:1.55;margin:0;">${escapeHtml(b.body || '')}</p>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    console.error('Notifications load error:', err);
    container.innerHTML = '<div class="lb-loading" style="color:var(--red);">⚠️ Failed to load announcements</div>';
  }
}

// ── 📋 TASKS SCREEN — always re-fetches tasks from DB, tracks completion ──
async function loadTasksScreen() {
  const list = document.getElementById('tasks-screen-list');
  const progFillEl = document.getElementById('tasks-screen-prog-fill');
  const progLabelEl = document.getElementById('tasks-screen-prog-label');
  if (!list) return;

  list.innerHTML = '<div class="lb-loading">Loading tasks...</div>';
  if (progLabelEl) progLabelEl.textContent = 'Loading...';

  try {
    const { data: taskData, error } = await sb
      .from('tasks')
      .select('*')
      .eq('is_active', true)
      .order('sort_order');

    if (error) throw error;
    const tasks = taskData || [];

    if (tasks.length === 0) {
      list.innerHTML = '<div class="lb-loading">No tasks right now. Come back soon! 🐾</div>';
      if (progLabelEl) progLabelEl.textContent = 'No tasks';
      return;
    }

    // Sync progress bar
    const doneCount = tasks.filter(t => completedTasks.has(String(t.id))).length;
    const pct = tasks.length > 0 ? (doneCount / tasks.length) * 100 : 0;
    if (progFillEl) progFillEl.style.width = pct + '%';
    if (progLabelEl) progLabelEl.textContent = `${doneCount} / ${tasks.length} completed`;

    list.innerHTML = '';
    tasks.forEach(task => {
      const done = completedTasks.has(String(task.id));
      const card = document.createElement('div');
      card.className = `task-card${done ? ' done' : ''}${task.type === 'wallet' ? ' wallet-card' : ''}`;

      if (task.type === 'wallet') {
        card.innerHTML = `
          <div class="wallet-row-top">
            <div class="task-icon">${getTaskIcon(task.title)}</div>
            <div class="task-info">
              <h3>${escapeHtml(task.title)}</h3>
              <p>${escapeHtml(task.description || 'Enter BEP-20 address')}</p>
              ${task.points > 0 ? `<p style="color:#ffc84a;font-weight:700;font-size:12px;margin-top:3px;">🎁 +${task.points} pts on completion</p>` : ''}
            </div>
            <div class="task-check">${done ? '✓' : ''}</div>
          </div>
          ${!done ? `
          <div class="wallet-row" style="display:flex;gap:8px;margin-top:10px;width:100%;">
            <input class="wallet-input" placeholder="0x…" id="ts-wi-${task.id}" maxlength="42" style="flex:1;padding:8px;border-radius:6px;border:1px solid var(--border);background:#000;color:#fff;"/>
            <button class="wallet-submit" onclick="tasksScreenSubmitWallet(${task.id}, ${task.points || 0})" style="background:var(--gold);color:#000;border:none;padding:8px 16px;font-weight:bold;border-radius:6px;cursor:pointer;">SAVE</button>
          </div>` : ''}`;
      } else {
        card.innerHTML = `
          <div class="task-icon">${getTaskIcon(task.title)}</div>
          <div class="task-info">
            <h3>${escapeHtml(task.title)}</h3>
            <p>${escapeHtml(task.description || 'Click GO to complete')}</p>
            ${task.points > 0 ? `<p style="color:#ffc84a;font-weight:700;font-size:12px;margin-top:3px;">🎁 +${task.points} pts on completion</p>` : ''}
          </div>
          ${done
            ? `<div class="task-check">✓</div>`
            : `<button class="task-btn" data-task-id="${task.id}" data-task-link="${escapeHtml(task.link || '')}" data-task-points="${task.points || 0}">GO →</button>`
          }`;
      }

      list.appendChild(card);
    });

    // Wire up GO buttons
    list.querySelectorAll('.task-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = parseInt(btn.getAttribute('data-task-id'));
        const link = btn.getAttribute('data-task-link');
        const points = parseInt(btn.getAttribute('data-task-points')) || 0;
        openTaskFromScreen(id, link, points);
      });
    });

  } catch (err) {
    console.error('Tasks screen load error:', err);
    list.innerHTML = '<div class="lb-loading" style="color:var(--red);">⚠️ Failed to load tasks</div>';
  }
}

async function openTaskFromScreen(taskId, link, points) {
  if (link && link !== 'null' && link !== '') {
    if (window.Telegram?.WebApp) {
      window.Telegram.WebApp.openLink(link);
    } else {
      window.open(link, '_blank');
    }
  }
  setTimeout(async () => {
    await markDoneAndRefreshScreen(taskId, points);
  }, 2000);
}

async function markDoneAndRefreshScreen(taskId, points) {
  completedTasks.add(String(taskId));
  const { error } = await sb
    .from('users')
    .update({ tasks_completed: Array.from(completedTasks) })
    .eq('telegram_id', tgUser.id);
  if (error) console.error('markDone error:', error);

  // Award points if the task has a reward
  if (points && points > 0) {
    await sb.from('scores').insert({
      telegram_id: tgUser.id,
      username: tgUser.username,
      score: points,
      source: 'task_reward',
    });
    toast(`✅ Task done! +${points} pts 🎉`);
  } else {
    toast('✅ Task completed!');
  }

  loadTasksScreen();
  renderOnboarding();
}

async function tasksScreenSubmitWallet(taskId, points) {
  const input = document.getElementById('ts-wi-' + taskId);
  const addr = input?.value?.trim() || '';
  if (!/^0x[0-9a-fA-F]{40}$/.test(addr)) {
    toast('⚠️ Enter a valid 42-character BSC address (0x…)');
    return;
  }
  const { error } = await sb.from('users').update({ wallet_address: addr }).eq('telegram_id', tgUser.id);
  if (error) { toast('❌ Failed to save wallet'); return; }
  if (currentUser) currentUser.wallet_address = addr;
  await markDoneAndRefreshScreen(taskId, points);
  updateSettingsPanel();
}

window.tasksScreenSubmitWallet = tasksScreenSubmitWallet;

init();

// ── 📢 SYSTEM BROADCAST ANNOUNCEMENT LOGIC ──

async function checkSystemBroadcasts() {
  const banner = document.getElementById('broadcast-banner');
  const titleEl = document.getElementById('broadcast-title');
  const bodyEl = document.getElementById('broadcast-body');
  
  if (!banner || !titleEl || !bodyEl) return;

  try {
    const { data, error } = await sb
      .from('broadcasts')
      .select('title, body, type, created_at')
      .order('created_at', { ascending: false })
      .limit(1);

    if (error) throw error;

    if (data && data.length > 0) {
      const activeNotice = data[0];

      // Check if user already closed this one this session
      const lastClosed = sessionStorage.getItem('last_closed_broadcast');
      if (lastClosed === activeNotice.created_at) return;

      titleEl.textContent = activeNotice.title.toUpperCase();
      bodyEl.textContent = activeNotice.body;
      banner.setAttribute('data-timestamp', activeNotice.created_at);

      // Show banner automatically on start screen
      banner.style.display = 'block';
    }
  } catch (err) {
    console.error("Failed fetching live announcements from Supabase:", err);
  }
}

// Function to let the user click '×' and hide the announcement box
function closeBroadcastBanner() {
  const banner = document.getElementById('broadcast-banner');
  if (banner) {
    banner.style.display = 'none';
    const timestamp = banner.getAttribute('data-timestamp');
    if (timestamp) {
      sessionStorage.setItem('last_closed_broadcast', timestamp);
    }
  }
}

window.closeBroadcastBanner = closeBroadcastBanner;
// ── LIVE REAL-TIME ADMIN COMMAND SYNC ──
setInterval(async () => {
  try {
    const { data } = await sb
      .from('game_config')
      .select('value')
      .eq('key', 'paused')
      .maybeSingle();

    if (data) {
      const adminSaysPaused = data.value === 'true';
      
      // If the admin pauses while the user is actively playing
      if (adminSaysPaused && !isPaused) {
        isPaused = true;
        // Halt the spawn timers and frames
        clearTimeout(spawnTimeout);
        if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
        stopBgMusic();
        
        // Show the pause/maintenance message on screen
        const msg = document.getElementById('maintenance-msg');
        if (msg) msg.style.display = 'block';
        
        // If they are deep inside the game screen, kick them back to start or show a block overlay
        showScreen('start'); 
        toast('⏸ The game has been paused by the admin for maintenance.', 4000);
      } 
      // If the admin unpauses the game
      else if (!adminSaysPaused && isPaused) {
        isPaused = false;
        const msg = document.getElementById('maintenance-msg');
        if (msg) msg.style.display = 'none';
        toast('▶️ The game is live again! Have fun!', 3000);
      }
    }
  } catch (err) {
    console.warn("Live status sync error:", err);
  }
}, 4000); // Polls every 4 seconds dynamically in the background