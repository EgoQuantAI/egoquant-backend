import faulthandler, sys, threading, time
import numpy as np
faulthandler.dump_traceback_later(80, exit=True)
stop = False
def burn():
    a = np.random.rand(700, 700)
    while not stop:
        a @ a
burners = [threading.Thread(target=burn, daemon=True) for _ in range(3)]
[t.start() for t in burners]
t0, n, worst = time.time(), 0, 0.0
while time.time() - t0 < 60:
    s = time.time()
    t = threading.Thread(target=lambda: None); t.start(); t.join()
    worst = max(worst, time.time() - s); n += 1
stop = True
print("threads started:", n, "worst start+join %.3fs" % worst)
