setlocal wrap linebreak autoread
autocmd FocusGained,BufEnter,CursorHold * checktime
call timer_start(1000, {-> execute('checktime')}, {'repeat': -1})
