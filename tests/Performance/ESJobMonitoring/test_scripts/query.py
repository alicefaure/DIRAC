"""
Performance test created using multi-mechnize to analyze time
for query processing with ElasticSearch.
"""

import random
import time

from DIRAC.WorkloadManagementSystem.DB.ElasticJobParametersDB import ElasticJobParametersDB


class Transaction:
    def __init__(self):

        self.elasticJobParametersDB = ElasticJobParametersDB()
        self.custom_timers = {}

    def run(self):

        start_time = time.time()

        for i in range(0, random.randint(1000, 3000)):

            jobID = random.randint(1, 1000)
            self.elasticJobParametersDB.getJobParameters(jobID)

        end_time = time.time()

        self.custom_timers["Execution_Time"] = end_time - start_time


if __name__ == "__main__":

    trans = Transaction()
    trans.run()
    print(trans.custom_timers)
