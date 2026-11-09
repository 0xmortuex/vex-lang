"""N-body simulation, the CPython reference for nbody.vx: same algorithm, same flat lists."""

import math
import sys

SOLAR_MASS = 39.47841760435743
DAYS = 365.24


def energy(x, y, z, vx, vy, vz, m):
    e = 0.0
    n = len(x)
    for i in range(n):
        e += 0.5 * m[i] * (vx[i] * vx[i] + vy[i] * vy[i] + vz[i] * vz[i])
        for j in range(i + 1, n):
            dx = x[i] - x[j]
            dy = y[i] - y[j]
            dz = z[i] - z[j]
            e -= m[i] * m[j] / math.sqrt(dx * dx + dy * dy + dz * dz)
    return e


def advance(x, y, z, vx, vy, vz, m, dt):
    n = len(x)
    for i in range(n):
        for j in range(i + 1, n):
            dx = x[i] - x[j]
            dy = y[i] - y[j]
            dz = z[i] - z[j]
            d2 = dx * dx + dy * dy + dz * dz
            mag = dt / (d2 * math.sqrt(d2))
            mi = m[i] * mag
            mj = m[j] * mag
            vx[i] = vx[i] - dx * mj
            vy[i] = vy[i] - dy * mj
            vz[i] = vz[i] - dz * mj
            vx[j] = vx[j] + dx * mi
            vy[j] = vy[j] + dy * mi
            vz[j] = vz[j] + dz * mi
    for i in range(n):
        x[i] = x[i] + dt * vx[i]
        y[i] = y[i] + dt * vy[i]
        z[i] = z[i] + dt * vz[i]


def main():
    steps = int(sys.argv[1])
    x = [0.0, 4.84143144246472082945e00, 8.34336671801458124085e00, 1.28943695621391310164e01, 1.53796971148509165498e01]
    y = [0.0, -1.16032004402742839362e00, 4.12479856412430479603e00, -1.51111514016986312525e01, -2.59193146099879641146e01]
    z = [0.0, -1.03622044471123109503e-01, -4.03523417825932481042e-01, -2.23307578892655733682e-01, 1.79258772950371181758e-01]
    vx = [0.0, 1.66007664274403694959e-03 * DAYS, -2.76742510726862411004e-03 * DAYS, 2.96460137564761618652e-03 * DAYS, 2.68067772490389322511e-03 * DAYS]
    vy = [0.0, 7.69901118419740425080e-03 * DAYS, 4.99852801234917238954e-03 * DAYS, 2.37847173959480950018e-03 * DAYS, 1.62824170038242295922e-03 * DAYS]
    vz = [0.0, -6.90460016972063023554e-05 * DAYS, 2.30417297573763262252e-05 * DAYS, -2.96589568540237556131e-05 * DAYS, -9.51592254519715870971e-05 * DAYS]
    m = [SOLAR_MASS, 9.54791938424326609124e-04 * SOLAR_MASS, 2.85885980666130812119e-04 * SOLAR_MASS, 4.36624404335156298006e-05 * SOLAR_MASS, 5.15138902046611451304e-05 * SOLAR_MASS]
    px = py = pz = 0.0
    for i in range(5):
        px += vx[i] * m[i]
        py += vy[i] * m[i]
        pz += vz[i] * m[i]
    vx[0] = 0.0 - px / SOLAR_MASS
    vy[0] = 0.0 - py / SOLAR_MASS
    vz[0] = 0.0 - pz / SOLAR_MASS
    print(int(energy(x, y, z, vx, vy, vz, m) * 1000000000.0))
    for _ in range(steps):
        advance(x, y, z, vx, vy, vz, m, 0.01)
    print(int(energy(x, y, z, vx, vy, vz, m) * 1000000000.0))


main()
