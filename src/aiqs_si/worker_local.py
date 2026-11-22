"""Local process-separation test runner. Not the remote production transport."""
import argparse
import time
from .store import Store
from .worker import Worker
from aiqs_si_device.synthetic import SyntheticDevice
from .outbox import CompletionOutbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = Worker(Store(args.database), SyntheticDevice(),
                    outbox=CompletionOutbox(args.database + ".outbox.sqlite"))
    while True:
        worker.run_one()
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
