/* ========================================
   RUSH VOTE — Simplified App Logic
   Flow: Enter address → Send 1 RUSH → Auto-detect deposit → Vote
   ======================================== */

(function () {
  'use strict';

  const CGI_BIN = window.location.origin;
  const POLL_ID = 'sol-chart';
  const DEPOSIT_ADDRESS = '7rH4WYQ9Y7UjmizQxvHmpgLyvsBZfArweE48DWrcyoXu';
  const RUSH_MINT = 'ZZdUjmm6stModTGwB7yQk9RphzbV6WYHMD5Wz7oPLAY';
  const SOLANA_RPC = 'https://solana-rpc.publicnode.com';
  const POLL_INTERVAL = 8000; // check every 8 seconds

  let address = null;
  let pollTimer = null;
  let voteWeight = 0;
  let txSig = '';

  // ── DOM ──
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  const stepAddress = $('#step-address');
  const stepDeposit = $('#step-deposit');
  const stepVote    = $('#step-vote');
  const stepDone    = $('#step-done');
  const walletInput = $('#wallet-input');
  const goBtn       = $('#go-btn');
  const addrError   = $('#address-error');
  const copyBtn     = $('#copy-btn');
  const depositStatus = $('#deposit-status');
  const votePower   = $('#vote-power');
  const solscanLink = $('#solscan-link');
  const votedDetail = $('#voted-detail');

  // ── Helpers ──
  function show(el) { el.classList.remove('hidden'); }
  function hide(el) { el.classList.add('hidden'); }
  function fmt(n) { return parseFloat(n).toLocaleString(undefined, { maximumFractionDigits: 2 }); }

  function isValidSolana(addr) {
    return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(addr);
  }

  async function api(path, method, body) {
    const opts = { method };
    if (body) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(CGI_BIN + '/vote' + path, opts);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    return data;
  }

  async function getRushBalance(wallet) {
    try {
      const res = await fetch(SOLANA_RPC, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jsonrpc: '2.0', id: 1,
          method: 'getTokenAccountsByOwner',
          params: [wallet, { mint: RUSH_MINT }, { encoding: 'jsonParsed', commitment: 'confirmed' }]
        })
      });
      const data = await res.json();
      if (data.result && data.result.value && data.result.value.length > 0) {
        return parseFloat(data.result.value[0].account.data.parsed.info.tokenAmount.uiAmount || 0);
      }
    } catch (e) { /* fall through */ }
    return 0;
  }

  function updateResults(r) {
    const total = r.total || 0;
    const yp = total > 0 ? Math.round((r.yes / total) * 100) : 0;
    const np = total > 0 ? Math.round((r.no / total) * 100) : 0;

    $('#yes-pct').textContent  = yp + '%';
    $('#no-pct').textContent   = np + '%';
    $('#yes-bar').style.width  = yp + '%';
    $('#no-bar').style.width   = np + '%';
    $('#yes-count').textContent = fmt(r.yes) + ' RUSH';
    $('#no-count').textContent  = fmt(r.no) + ' RUSH';
    $('#yes-voters').textContent = (r.yes_voters || 0) + ' voter' + ((r.yes_voters || 0) === 1 ? '' : 's');
    $('#no-voters').textContent  = (r.no_voters || 0) + ' voter' + ((r.no_voters || 0) === 1 ? '' : 's');
    $('#total-votes').textContent = fmt(total) + ' RUSH';
    $('#total-voters').textContent = (r.total_voters || 0) + ' voter' + ((r.total_voters || 0) === 1 ? '' : 's');
  }

  // ── Step 1: Enter address ──
  async function handleGo() {
    const addr = walletInput.value.trim();
    hide(addrError);

    if (!addr || !isValidSolana(addr)) {
      addrError.textContent = 'Enter a valid Solana address';
      show(addrError);
      return;
    }

    goBtn.disabled = true;
    goBtn.innerHTML = '<span class="spinner"></span>';

    try {
      // Check if already voted
      const data = await api('/verify', 'POST', { address: addr });
      address = addr;

      if (data.existing_vote) {
        voteWeight = data.vote_weight;
        showDone(data.existing_vote, data.vote_weight);
        return;
      }

      if (!data.verified) {
        addrError.textContent = 'No $RUSH found in this wallet. Buy some RUSH first.';
        show(addrError);
        return;
      }

      // Store the verified balance from backend
      voteWeight = parseFloat(data.balance) || 0;

      // Move to deposit step
      hide(stepAddress);
      show(stepDeposit);
      startDepositPolling();

    } catch (e) {
      addrError.textContent = e.message || 'Something went wrong';
      show(addrError);
    } finally {
      goBtn.disabled = false;
      goBtn.textContent = 'Go';
    }
  }

  // ── Step 2: Auto-poll for deposit ──
  function startDepositPolling() {
    checkDeposit(); // check immediately
    pollTimer = setInterval(checkDeposit, POLL_INTERVAL);
  }

  async function checkDeposit() {
    try {
      const data = await api('/check-deposit', 'POST', { address: address });

      if (data.deposit_confirmed) {
        clearInterval(pollTimer);
        pollTimer = null;
        txSig = data.tx_signature || '';

        // Use the best balance available:
        // 1. Backend check-deposit balance (freshest, just fetched)
        // 2. Already-stored balance from verify step
        var backendBalance = parseFloat(data.vote_weight) || parseFloat(data.current_balance) || 0;
        if (backendBalance > 0) {
          voteWeight = backendBalance;
        }
        // If still 0, try client-side RPC as last resort
        if (voteWeight <= 0) {
          try {
            var clientBalance = await getRushBalance(address);
            if (clientBalance > 0) voteWeight = clientBalance;
          } catch (e) { /* use what we have */ }
        }

        // Transition to vote step
        depositStatus.innerHTML = '<span style="color:var(--color-success)">✓ Deposit confirmed</span>';

        setTimeout(function () {
          hide(stepDeposit);
          votePower.textContent = fmt(voteWeight);
          solscanLink.href = 'https://solscan.io/account/' + address;
          show(stepVote);
        }, 800);
      }
    } catch (e) {
      // Silently retry on next interval
    }
  }

  // ── Step 3: Vote ──
  async function castVote(choice) {
    // Disable both buttons
    $$('.vote-btn').forEach(function (b) { b.disabled = true; });
    const btn = $('[data-vote="' + choice + '"]');
    btn.innerHTML = '<span class="spinner"></span>';

    try {
      const data = await api('/vote', 'POST', {
        address: address,
        vote: choice,
        poll_id: POLL_ID,
        tx_signature: txSig
      });

      if (data.success) {
        updateResults(data.results);
        showDone(choice, data.vote_weight);
      }
    } catch (e) {
      // Re-enable buttons
      $$('.vote-btn').forEach(function (b) { b.disabled = false; });
      btn.innerHTML = choice === 'yes'
        ? 'YES<span class="vote-btn-label">Add SOL</span>'
        : 'NO<span class="vote-btn-label">Keep BTC Only</span>';

      if (e.message && e.message.includes('already voted')) {
        showDone(choice, voteWeight);
      }
    }
  }

  // ── Done ──
  function showDone(choice, weight) {
    hide(stepAddress);
    hide(stepDeposit);
    hide(stepVote);
    show(stepDone);
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

    const label = choice === 'yes' ? 'YES — Add SOL' : 'NO — Keep BTC Only';
    votedDetail.innerHTML =
      'You voted <strong>' + label + '</strong><br>' +
      '<span class="vote-weight-inline">' + fmt(weight || 0) + ' RUSH</span> vote weight';
  }

  // ── Bind events ──
  goBtn.addEventListener('click', handleGo);
  walletInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); handleGo(); }
  });

  copyBtn.addEventListener('click', function () {
    var ta = document.createElement('textarea');
    ta.value = DEPOSIT_ADDRESS;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      copyBtn.textContent = 'Copied';
      copyBtn.classList.add('copied');
      setTimeout(function () { copyBtn.textContent = 'Copy'; copyBtn.classList.remove('copied'); }, 2000);
    } catch (e) { /* fallback: user can manually copy */ }
    document.body.removeChild(ta);
  });

  $$('.vote-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      castVote(btn.dataset.vote);
    });
  });

  // ── Load results on page load ──
  api('/results?poll_id=' + POLL_ID, 'GET').then(updateResults).catch(function () {});

})();
