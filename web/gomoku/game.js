class GomokuGame {
    constructor() {
        this.boardSize = 15;
        this.board = [];
        this.currentPlayer = 'black';
        this.gameOver = false;
        this.aiEnabled = false;
        this.winningCells = [];
        this.maxDepth = 6;

        // 学习系统
        this.moveHistory = [];
        this.loadKnowledge();

        this.init();
    }

    init() {
        // 老内核可能没有 AbortController —— 没有就不绑 signal（关遮罩时靠 gameOver 挡）
        this._ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        this._aiTimer = null;
        this.createBoard();
        this.bindEvents();
        this.resetGame();
    }

    destroy() {
        this.gameOver = true;
        if (this._aiTimer) {
            clearTimeout(this._aiTimer);
            this._aiTimer = null;
        }
        if (this._ac) {
            this._ac.abort();
            this._ac = null;
        }
        this.hideModal();
    }

    createBoard() {
        const boardElement = document.getElementById('board');
        boardElement.innerHTML = '';
        for (let i = 0; i < this.boardSize * this.boardSize; i++) {
            const cell = document.createElement('div');
            cell.className = 'cell';
            cell.dataset.index = i;
            boardElement.appendChild(cell);
        }
    }

    bindEvents() {
        const signal = this._ac && this._ac.signal;
        const opts = signal ? { signal } : undefined;
        document.getElementById('board').addEventListener('click', (e) => {
            if (e.target.classList.contains('cell') && !this.gameOver) {
                this.handleClick(parseInt(e.target.dataset.index));
            }
        }, opts);
        document.getElementById('restart').addEventListener('click', () => this.resetGame(), opts);
        document.getElementById('modal-restart').addEventListener('click', () => {
            this.hideModal();
            this.resetGame();
        }, opts);
        document.getElementById('ai-toggle').addEventListener('click', (e) => {
            this.aiEnabled = !this.aiEnabled;
            e.target.textContent = `AI 对战: ${this.aiEnabled ? '开' : '关'}`;
            e.target.classList.toggle('active', this.aiEnabled);
            this.resetGame();
        }, opts);
    }

    resetGame() {
        this.board = Array(this.boardSize * this.boardSize).fill(null);
        this.currentPlayer = 'black';
        this.gameOver = false;
        this.winningCells = [];
        this.moveHistory = [];
        document.querySelectorAll('#board .piece').forEach(p => p.remove());
        document.querySelectorAll('#board .cell').forEach(c => c.classList.remove('occupied'));
        this.updateStatus();
    }

    handleClick(index) {
        if (this.board[index] !== null) return;

        this.moveHistory.push({
            pos: index,
            player: this.currentPlayer,
            turn: this.moveHistory.length
        });

        this.board[index] = this.currentPlayer;
        const cell = document.querySelector(`#board [data-index="${index}"]`);
        cell.classList.add('occupied');
        const piece = document.createElement('div');
        piece.className = `piece ${this.currentPlayer}`;
        cell.appendChild(piece);

        if (this.checkWin(index)) {
            this.showWinner();
            return;
        }

        if (this.board.every(c => c !== null)) {
            this.gameOver = true;
            document.getElementById('winner-text').textContent = '平局!';
            this.showModal();
            return;
        }

        this.currentPlayer = this.currentPlayer === 'black' ? 'white' : 'black';
        this.updateStatus();

        if (this.aiEnabled && this.currentPlayer === 'white' && !this.gameOver) {
            if (this._aiTimer) clearTimeout(this._aiTimer);
            this._aiTimer = setTimeout(() => {
                this._aiTimer = null;
                this.aiMove();
            }, 50);
        }
    }

    updateStatus() {
        const el = document.getElementById('current-player');
        el.textContent = this.currentPlayer === 'black' ? '黑棋' : '白棋';
        el.className = this.currentPlayer;

        const learnInfo = document.getElementById('learn-info');
        if (learnInfo) {
            const learned = Object.keys(this.knowledge.counterMoves || {}).length;
            const wins = this.knowledge.stats?.aiWins || 0;
            const losses = this.knowledge.stats?.playerWins || 0;
            learnInfo.textContent = `已学习 ${learned} 种应对 | AI胜${wins}局 输${losses}局`;
        }
    }

    checkWin(index) {
        const row = Math.floor(index / this.boardSize);
        const col = index % this.boardSize;
        const player = this.board[index];
        const dirs = [[0,1], [1,0], [1,1], [1,-1]];

        for (const [dr, dc] of dirs) {
            let count = 1;
            for (let i = 1; i <= 4; i++) {
                const r = row + dr*i, c = col + dc*i;
                if (r >= 0 && r < this.boardSize && c >= 0 && c < this.boardSize &&
                    this.board[r * this.boardSize + c] === player) count++;
                else break;
            }
            for (let i = 1; i <= 4; i++) {
                const r = row - dr*i, c = col - dc*i;
                if (r >= 0 && r < this.boardSize && c >= 0 && c < this.boardSize &&
                    this.board[r * this.boardSize + c] === player) count++;
                else break;
            }
            if (count >= 5) {
                this.winningCells = [];
                for (let i = -4; i <= 4; i++) {
                    const r = row + dr*i, c = col + dc*i;
                    if (r >= 0 && r < this.boardSize && c >= 0 && c < this.boardSize) {
                        const idx = r * this.boardSize + c;
                        if (this.board[idx] === player) this.winningCells.push(idx);
                    }
                }
                return true;
            }
        }
        return false;
    }

    showWinner() {
        this.gameOver = true;
        this.winningCells.forEach(idx => {
            const cell = document.querySelector(`#board [data-index="${idx}"]`);
            const piece = cell.querySelector('.piece');
            if (piece) piece.classList.add('winning');
        });
        const winner = this.currentPlayer === 'black' ? '黑棋' : '白棋';
        document.getElementById('winner-text').textContent = `${winner}获胜!`;
        this.showModal();

        // 学习
        if (this.aiEnabled) {
            if (this.currentPlayer === 'black') {
                this.learnFromDefeat();
            } else {
                this.recordWin();
            }
        }
    }

    showModal() { document.getElementById('gomoku-win').classList.remove('hidden'); }
    hideModal() { document.getElementById('gomoku-win').classList.add('hidden'); }

    // ==================== 学习系统 ====================

    loadKnowledge() {
        const saved = localStorage.getItem('cbg-gomoku-ai-knowledge');
        if (saved) {
            this.knowledge = JSON.parse(saved);
        } else {
            this.knowledge = {
                counterMoves: {},    // 对手开局的破解方法
                badResponses: {},    // 失败的应对记录
                openingPatterns: {}, // 开局模式分析
                stats: { aiWins: 0, playerWins: 0 }
            };
        }
    }

    saveKnowledge() {
        localStorage.setItem('cbg-gomoku-ai-knowledge', JSON.stringify(this.knowledge));
    }

    getPatternKey(moves) {
        // 获取开局序列的标准化key（考虑镜像和旋转）
        return moves.slice(0, 8).map(m => m.pos).join('-');
    }

    recordWin() {
        this.knowledge.stats.aiWins++;
        this.saveKnowledge();
    }

    learnFromDefeat() {
        this.knowledge.stats.playerWins++;
        console.log('=== AI 正在分析失败 ===');

        // 分析玩家的开局模式
        const playerMoves = this.moveHistory.filter(m => m.player === 'black');
        const patternKey = this.getPatternKey(this.moveHistory);

        // 找关键转折点：AI 在哪一步开始走向失败
        let criticalTurn = -1;
        const aiMoves = this.moveHistory.filter(m => m.player === 'white');

        // 分析每一步AI的应对
        for (let i = 0; i < aiMoves.length; i++) {
            const aiMove = aiMoves[i];
            const boardBefore = this.reconstructBoardBefore(aiMove.turn);

            // 记录这个应对是失败的
            const boardKey = this.boardToKey(boardBefore);
            if (!this.knowledge.badResponses[boardKey]) {
                this.knowledge.badResponses[boardKey] = [];
            }
            this.knowledge.badResponses[boardKey].push(aiMove.pos);

            // 尝试找更好的应对
            const betterMove = this.findBetterResponse(boardBefore, 8);
            if (betterMove !== -1 && betterMove !== aiMove.pos) {
                this.knowledge.counterMoves[boardKey] = betterMove;
                console.log(`找到更好应对: 局面 -> 位置 ${betterMove}`);
                criticalTurn = i;
            }
        }

        // 特殊处理：记录完整开局破解
        if (playerMoves.length <= 6) {
            // 这是一个短开局杀，需要重点学习
            const shortPattern = playerMoves.slice(0, 4).map(m => m.pos).join('-');
            const firstAiMove = aiMoves[0];
            if (firstAiMove) {
                const boardBeforeFirst = this.reconstructBoardBefore(1);
                const betterFirst = this.findBetterResponse(boardBeforeFirst, 10);
                if (betterFirst !== -1) {
                    this.knowledge.counterMoves[`opening_${shortPattern}`] = betterFirst;
                    console.log(`学会破解开局 ${shortPattern} -> 第一步应对 ${betterFirst}`);
                }
            }
        }

        this.saveKnowledge();
        console.log('=== 学习完成 ===');
        this.updateStatus();
    }

    reconstructBoardBefore(turn) {
        const board = Array(this.boardSize * this.boardSize).fill(null);
        for (let i = 0; i < turn && i < this.moveHistory.length; i++) {
            const move = this.moveHistory[i];
            board[move.pos] = move.player;
        }
        return board;
    }

    boardToKey(board) {
        return board.map(c => c === 'black' ? 'B' : c === 'white' ? 'W' : '.').join('');
    }

    keyToBoard(key) {
        return key.split('').map(c => c === 'B' ? 'black' : c === 'W' ? 'white' : null);
    }

    findBetterResponse(boardBefore, searchDepth) {
        // 深度搜索更好的应对
        const tempBoard = this.board;
        this.board = boardBefore;

        const empty = this.getEmptyPoints();
        if (empty.length === 0) {
            this.board = tempBoard;
            return -1;
        }

        // Monte Carlo 模拟搜索
        let bestMove = -1;
        let bestScore = -Infinity;

        for (const pos of empty.slice(0, 25)) {
            const boardKey = this.boardToKey(boardBefore);
            const badList = this.knowledge.badResponses[boardKey] || [];
            if (badList.includes(pos)) continue;

            // 模拟这个应对后的结果
            this.board[pos] = 'white';

            // 检查是否能直接获胜
            if (this.checkWinAt(pos)) {
                this.board = tempBoard;
                return pos;
            }

            // 深度评估
            const score = this.simulateGame(pos, searchDepth, 'black');

            this.board[pos] = null;

            if (score > bestScore) {
                bestScore = score;
                bestMove = pos;
            }
        }

        this.board = tempBoard;
        return bestMove;
    }

    simulateGame(aiMove, depth, nextPlayer) {
        // 模拟后续对局，评估AI的胜率
        if (depth <= 0) return this.evalBoard();

        // 检查当前局面
        if (this.checkWinAt(aiMove)) {
            return this.board[aiMove] === 'white' ? 100000 : -100000;
        }

        const empty = this.getEmptyPoints();
        if (empty.length === 0) return 0;

        // 检查威胁
        let threats = 0;
        for (const pos of empty) {
            this.board[pos] = 'black';
            if (this.checkWinAt(pos)) threats += 10000;
            else if (this.hasLiveFour(pos)) threats += 1000;
            else if (this.hasDoubleLiveThree(pos)) threats += 500;
            this.board[pos] = null;
        }

        // 简化：如果对手有太大威胁，这个应对不好
        if (threats > 5000 && nextPlayer === 'black') {
            return -threats;
        }

        // 简化的 Minimax
        if (nextPlayer === 'black') {
            // 对手回合，找最大威胁
            let maxThreat = 0;
            for (const pos of empty.slice(0, 15)) {
                this.board[pos] = 'black';
                const threat = this.evalThreat(pos);
                this.board[pos] = null;
                maxThreat = Math.max(maxThreat, threat);
            }
            return this.evalBoard() - maxThreat;
        } else {
            // AI回合，找最佳应对
            let bestScore = -Infinity;
            for (const pos of empty.slice(0, 15)) {
                this.board[pos] = 'white';
                const score = this.evalBoard();
                this.board[pos] = null;
                bestScore = Math.max(bestScore, score);
            }
            return bestScore;
        }
    }

    evalThreat(pos) {
        const patterns = this.getPatterns(pos, this.board[pos]);
        return patterns.reduce((s, p) => s + p.score, 0);
    }

    hasLiveFour(pos) {
        const player = this.board[pos];
        if (!player) return false;
        const patterns = this.getPatterns(pos, player);
        return patterns.some(p => p.type === 'LIVE_FOUR');
    }

    hasDoubleLiveThree(pos) {
        const player = this.board[pos];
        if (!player) return false;
        const patterns = this.getPatterns(pos, player);
        const liveThrees = patterns.filter(p => p.type === 'LIVE_THREE').length;
        return liveThrees >= 2;
    }

    // ==================== AI ====================

    aiMove() {
        const move = this.findBestMove();
        if (move !== -1 && this.board[move] === null) {
            this.handleClick(move);
        }
    }

    findBestMove() {
        // 空棋盘下中心
        if (this.board.every(c => c === null)) {
            return 7 * this.boardSize + 7;
        }

        // 查学到的应对
        const boardKey = this.boardToKey(this.board);
        const learned = this.knowledge.counterMoves[boardKey];
        if (learned !== undefined && this.board[learned] === null) {
            const badList = this.knowledge.badResponses[boardKey] || [];
            if (!badList.includes(learned)) {
                console.log(`使用学到的应对: ${learned}`);
                return learned;
            }
        }

        // 查开局破解库
        const patternKey = this.getPatternKey(this.moveHistory);
        const openingCounter = this.knowledge.counterMoves[`opening_${patternKey}`];
        if (openingCounter !== undefined && this.board[openingCounter] === null) {
            console.log(`使用开局破解: ${openingCounter}`);
            return openingCounter;
        }

        const empty = this.getEmptyPoints();

        // 过滤失败应对
        const badList = this.knowledge.badResponses[boardKey] || [];
        const safeMoves = empty.filter(pos => !badList.includes(pos));
        const candidates = safeMoves.length > 0 ? safeMoves : empty;

        // 1. 直接获胜
        for (const pos of candidates) {
            if (this.wouldWin(pos, 'white')) return pos;
        }

        // 2. 堵对手获胜
        for (const pos of candidates) {
            if (this.wouldWin(pos, 'black')) return pos;
        }

        // 3. 制造活四
        for (const pos of candidates) {
            if (this.wouldMakeLiveFour(pos, 'white')) return pos;
        }

        // 4. 堵对手活四
        for (const pos of candidates) {
            if (this.wouldMakeLiveFour(pos, 'black')) return pos;
        }

        // 5. 制造双活三
        for (const pos of candidates) {
            if (this.wouldMakeDoubleLiveThree(pos, 'white')) return pos;
        }

        // 6. 堵对手双活三
        for (const pos of candidates) {
            if (this.wouldMakeDoubleLiveThree(pos, 'black')) return pos;
        }

        // 7. 深度搜索
        return this.minimaxSearch(this.maxDepth, candidates);
    }

    getEmptyPoints() {
        const points = [];
        const visited = new Set();

        for (let i = 0; i < this.board.length; i++) {
            if (this.board[i] !== null) {
                const row = Math.floor(i / this.boardSize);
                const col = i % this.boardSize;
                for (let dr = -2; dr <= 2; dr++) {
                    for (let dc = -2; dc <= 2; dc++) {
                        const r = row + dr, c = col + dc;
                        if (r >= 0 && r < this.boardSize && c >= 0 && c < this.boardSize) {
                            const idx = r * this.boardSize + c;
                            if (this.board[idx] === null && !visited.has(idx)) {
                                points.push(idx);
                                visited.add(idx);
                            }
                        }
                    }
                }
            }
        }
        return points;
    }

    wouldWin(pos, player) {
        this.board[pos] = player;
        const win = this.checkWinAt(pos);
        this.board[pos] = null;
        return win;
    }

    checkWinAt(pos) {
        const player = this.board[pos];
        if (!player) return false;
        const row = Math.floor(pos / this.boardSize);
        const col = pos % this.boardSize;
        const dirs = [[0,1], [1,0], [1,1], [1,-1]];

        for (const [dr, dc] of dirs) {
            let count = 1;
            for (let i = 1; i <= 4; i++) {
                const r = row + dr*i, c = col + dc*i;
                if (r >= 0 && r < this.boardSize && c >= 0 && c < this.boardSize &&
                    this.board[r * this.boardSize + c] === player) count++;
                else break;
            }
            for (let i = 1; i <= 4; i++) {
                const r = row - dr*i, c = col - dc*i;
                if (r >= 0 && r < this.boardSize && c >= 0 && c < this.boardSize &&
                    this.board[r * this.boardSize + c] === player) count++;
                else break;
            }
            if (count >= 5) return true;
        }
        return false;
    }

    wouldMakeLiveFour(pos, player) {
        this.board[pos] = player;
        const patterns = this.getPatterns(pos, player);
        this.board[pos] = null;
        return patterns.some(p => p.type === 'LIVE_FOUR');
    }

    wouldMakeDoubleLiveThree(pos, player) {
        this.board[pos] = player;
        const patterns = this.getPatterns(pos, player);
        this.board[pos] = null;
        const liveThrees = patterns.filter(p => p.type === 'LIVE_THREE').length;
        const rushFours = patterns.filter(p => p.type === 'RUSH_FOUR').length;
        return liveThrees >= 2 || (liveThrees >= 1 && rushFours >= 1);
    }

    getPatterns(pos, player) {
        const row = Math.floor(pos / this.boardSize);
        const col = pos % this.boardSize;
        const dirs = [[0,1], [1,0], [1,1], [1,-1]];
        const patterns = [];

        for (const [dr, dc] of dirs) {
            let count = 1, block = 0;

            for (let i = 1; i <= 5; i++) {
                const r = row + dr*i, c = col + dc*i;
                if (r < 0 || r >= this.boardSize || c < 0 || c >= this.boardSize) {
                    block++; break;
                }
                const idx = r * this.boardSize + c;
                if (this.board[idx] === player) count++;
                else if (this.board[idx] === null) break;
                else { block++; break; }
            }

            for (let i = 1; i <= 5; i++) {
                const r = row - dr*i, c = col - dc*i;
                if (r < 0 || r >= this.boardSize || c < 0 || c >= this.boardSize) {
                    block++; break;
                }
                const idx = r * this.boardSize + c;
                if (this.board[idx] === player) count++;
                else if (this.board[idx] === null) break;
                else { block++; break; }
            }

            patterns.push(this.classify(count, block));
        }
        return patterns;
    }

    classify(count, block) {
        if (count >= 5) return { type: 'FIVE', score: 1000000 };
        if (block >= 2) return { type: 'DEAD', score: 0 };

        if (count === 4) {
            if (block === 0) return { type: 'LIVE_FOUR', score: 100000 };
            return { type: 'RUSH_FOUR', score: 10000 };
        }
        if (count === 3) {
            if (block === 0) return { type: 'LIVE_THREE', score: 5000 };
            return { type: 'SLEEP_THREE', score: 500 };
        }
        if (count === 2) {
            if (block === 0) return { type: 'LIVE_TWO', score: 200 };
            return { type: 'SLEEP_TWO', score: 50 };
        }
        if (count === 1 && block === 0) return { type: 'LIVE_ONE', score: 10 };
        return { type: 'NONE', score: 0 };
    }

    minimaxSearch(depth, candidates) {
        const empty = candidates || this.getEmptyPoints();
        if (empty.length === 0) return -1;

        const boardKey = this.boardToKey(this.board);
        const badList = this.knowledge.badResponses[boardKey] || [];
        const safeMoves = empty.filter(pos => !badList.includes(pos));
        const moves = safeMoves.length > 0 ? safeMoves : empty;

        const scored = moves.map(pos => ({
            pos,
            score: this.evalPos(pos, 'white') * 1.2 + this.evalPos(pos, 'black')
        })).sort((a, b) => b.score - a.score).slice(0, 20);

        let bestPos = -1;
        let bestScore = -Infinity;

        for (const { pos } of scored) {
            this.board[pos] = 'white';
            const score = this.minimax(depth - 1, -Infinity, Infinity, false);
            this.board[pos] = null;

            if (score > bestScore) {
                bestScore = score;
                bestPos = pos;
            }
        }

        return bestPos;
    }

    minimax(depth, alpha, beta, isMax) {
        for (let i = 0; i < this.board.length; i++) {
            if (this.board[i] && this.checkWinAt(i)) {
                return this.board[i] === 'white' ? 1000000 : -1000000;
            }
        }

        if (depth === 0) return this.evalBoard();

        const empty = this.getEmptyPoints();
        if (empty.length === 0) return 0;

        const boardKey = this.boardToKey(this.board);
        const badList = this.knowledge.badResponses[boardKey] || [];
        const safeMoves = empty.filter(pos => !badList.includes(pos));
        const moves = safeMoves.length > 0 ? safeMoves : empty;

        const scored = moves.map(pos => ({
            pos,
            score: this.evalPos(pos, isMax ? 'white' : 'black')
        })).sort((a, b) => b.score - a.score).slice(0, 12);

        if (isMax) {
            let max = -Infinity;
            for (const { pos } of scored) {
                this.board[pos] = 'white';
                const val = this.minimax(depth - 1, alpha, beta, false);
                this.board[pos] = null;
                max = Math.max(max, val);
                alpha = Math.max(alpha, val);
                if (beta <= alpha) break;
            }
            return max;
        } else {
            let min = Infinity;
            for (const { pos } of scored) {
                this.board[pos] = 'black';
                const val = this.minimax(depth - 1, alpha, beta, true);
                this.board[pos] = null;
                min = Math.min(min, val);
                beta = Math.min(beta, val);
                if (beta <= alpha) break;
            }
            return min;
        }
    }

    evalPos(pos, player) {
        this.board[pos] = player;
        const patterns = this.getPatterns(pos, player);
        let score = patterns.reduce((s, p) => s + p.score, 0);
        this.board[pos] = null;

        const row = Math.floor(pos / this.boardSize);
        const col = pos % this.boardSize;
        score += (7 - Math.abs(row - 7)) + (7 - Math.abs(col - 7));
        return score;
    }

    evalBoard() {
        let white = 0, black = 0;
        for (let i = 0; i < this.board.length; i++) {
            if (this.board[i] === 'white') white += this.pointScore(i, 'white');
            else if (this.board[i] === 'black') black += this.pointScore(i, 'black');
        }
        return white - black;
    }

    pointScore(pos, player) {
        const patterns = this.getPatterns(pos, player);
        return patterns.reduce((s, p) => s + p.score, 0);
    }
}

/* 彩蛋挂载口：不在 script 加载时开局 —— 只在遮罩打开时 mount。 */
let _gomokuGame = null;
function mountGomoku() {
    if (_gomokuGame) return _gomokuGame;
    _gomokuGame = new GomokuGame();
    return _gomokuGame;
}
function destroyGomoku() {
    if (!_gomokuGame) return;
    _gomokuGame.destroy();
    _gomokuGame = null;
}