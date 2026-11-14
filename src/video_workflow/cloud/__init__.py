"""Cloud control room: the dashboard on Vercel, backed by Postgres, kept in sync by the worker.

    worker (daemon, ffmpeg, Higgsfield, publishers)  ──push snapshot + media──▶  Vercel app ◀── you (phone/laptop)
                                                     ◀──pull commands─────────   (Neon Postgres)

The worker never needs to accept inbound connections: it pushes state and pulls the commands you
issue from the cloud dashboard (approve, render final, reject, retry, re-render, publish, ideas).
"""
