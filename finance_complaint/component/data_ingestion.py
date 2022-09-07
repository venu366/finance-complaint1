from finance_complaint.config import FinanceConfig
from finance_complaint.entity.config_entity import DataIngestionConfig
from finance_complaint.exception import FinanceException
from finance_complaint.entity.spark_manager import spark_session
import os, sys
from finance_complaint.entity.metadata_entity import DataIngestionMetadata
import pandas as pd
from finance_complaint.entity.artifact_entity import DataIngestionArtifact
from finance_complaint.logger import logger
import requests, json
from typing import List
from collections import namedtuple
import time
import re
import uuid

DownloadUrl = namedtuple("DownloadUrl", ["url", "file_path", "n_retry"])



class DataIngestion:

    def __init__(self, data_ingestion_config: DataIngestionConfig  , n_retry: int = 5, n_month_interval: int = 3):
        """
        data_ingestion_config: Data Ingestion config
        n_retry: Number of retry filed should be tried to download in case of failure encountered
        n_month_interval: n month data will be downloded
        """
        try:
            logger.info(f"{'>>'*20}Starting data ingestion.{'<<'*20}")
            self.data_ingestion_config = data_ingestion_config
            self.failed_download_urls: List[DownloadUrl] = []
            self.n_retry = n_retry
            self.n_month_interval = n_month_interval

        except Exception as e:
            raise FinanceException(e, sys)

    def download_monthly_files(self, n_month_interval_url: int = None) -> List[DownloadUrl]:
        """
        n_month_interval_url: if not provided then information default value will be set
        =======================================================================================
        returns: List of DownloadUrl = namedtuple("DownloadUrl", ["url", "file_path", "n_retry"])
        """
        try:
            if n_month_interval_url is None:
                n_month_interval_url = self.n_month_interval
            
            #month interval 
            month_interval = list(pd.date_range(start=self.data_ingestion_config.from_date,
                                                end=self.data_ingestion_config.to_date,
                                                freq="m").astype('str'))

            download_urls = []
            for index in range(n_month_interval_url, len(month_interval), n_month_interval_url):
                from_date, to_date = month_interval[index - n_month_interval_url], month_interval[index]
                logger.info(f"Generating data download url between {from_date} and {to_date}")
                url = f"https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/" \
                      f"?date_received_max={to_date}&date_received_min={from_date}" \
                      f"&field=all&format=json"

                logger.info(f"Url: {url}")
                file_name = f"{self.data_ingestion_config.file_name}_{from_date}_{to_date}.json"
                file_path = os.path.join(self.data_ingestion_config.download_dir, file_name)
                download_url = DownloadUrl(url=url, file_path=file_path, n_retry=self.n_retry)
                download_urls.append(download_url)
                self.download_data(download_url=download_url)
            logger.info(download_urls)
            return download_urls
        except Exception as e:
            raise FinanceException(e, sys)

    def convert_files_to_parquet(self, data_dir=None, json_data_dir=None, output_file_name=None)->str:
        """
        downloaded files will be converted and merged into single parquet file
        json_data_dir: downloaded json file directory
        data_dir: converted and combined file will be generated in data_dir
        output_file_name: output file name 
        =======================================================================================
        returns output_file_path
        """
        try:
            if json_data_dir is None:
                json_data_dir = self.data_ingestion_config.download_dir

            if data_dir is None:
                data_dir = self.data_ingestion_config.data_dir

            if output_file_name is None:
                output_file_name = self.data_ingestion_config.file_name

            os.makedirs(data_dir, exist_ok=True)

            file_path = os.path.join(data_dir, f"{output_file_name}")

            logger.info(f"Parquet file will be created at: {file_path}")

            if not os.path.exists(json_data_dir):
                
                return file_path

            for file_name in os.listdir(json_data_dir):
                df = spark_session.read.json(os.path.join(json_data_dir,file_name))
                df.write.mode('append').parquet(file_path)

            return file_path
        except Exception as e:
            raise FinanceException(e, sys)

    def retry_download_data(self, data, download_url: DownloadUrl):
        """
        This function help to avoid failure as it help to download failed file again
        
        data:failed response
        download_url: DownloadUrl
        """
        try:
            # if retry still possible try else return the response
            if download_url.n_retry == 0:
                self.failed_download_urls.append(download_url)
                logger.info(f"Unable to download file {download_url.url}")
                return

            # to handle throatling requestion and can be slove if we wait for some second.
            content = data.content.decode("utf-8")
            wait_second = re.findall(r'\d+', content)

            if len(wait_second) > 0:
                time.sleep(int(wait_second[0]) + 2)

            # Writing response to understand why request was failed
            failed_file_path = os.path.join(self.data_ingestion_config.failed_dir,
                                            os.path.basename(download_url.file_path))
            os.makedirs(self.data_ingestion_config.failed_dir, exist_ok=True)
            with open(failed_file_path, "wb") as file_obj:
                file_obj.write(data.content)

            # calling download function again to retry
            download_url = DownloadUrl(download_url.url, file_path=download_url.file_path,
                                       n_retry=download_url.n_retry - 1)
            self.download_data(download_url=download_url)
        except Exception as e:
            raise FinanceException(e, sys)

    def download_data(self, download_url: DownloadUrl):
        try:
            logger.info(f"Starting download operation: {download_url}")
            download_dir = os.path.dirname(download_url.file_path)

            #creating download directory
            os.makedirs(download_dir, exist_ok=True)


            #downloading data
            data = requests.get(download_url.url, params={'User-agent': f'your bot {uuid.uuid4()}'})

            try:
                logger.info(f"Started writing downlaoded data into json file: {download_url.file_path}")
                #saving downloaded data into hard disk
                with open(download_url.file_path, "w") as file_obj:
                    finance_complaint_data = list(map(lambda x: x["_source"],
                                                      filter(lambda x: "_source" in x.keys(),
                                                             json.loads(data.content)))
                                                  )

                    json.dump(finance_complaint_data, file_obj)
                logger.info(f"Downloaded data has been written into file: {download_url.file_path}")
            except Exception as e:
                logger.info("Failed to download hence retry again.")
                # removing file failed file exist
                if os.path.exists(download_url.file_path):
                    os.remove(download_url.file_path)
                self.retry_download_data(data, download_url=download_url)

        except Exception as e:
            logger.info(e)
            raise FinanceException(e, sys)

    def write_metadata(self,file_path:str)->None:
        """
        This function help us to update metadata information 
        so that we can avoid redundant download and merging.

        """
        try:
            logger.info(f"Writing metadata info into metadata file.")
            metadata_info = DataIngestionMetadata(metadata_file_path=self.data_ingestion_config.metadata_file_path)

            metadata_info.write_metadata_info(from_date=self.data_ingestion_config.from_date,
            to_date=self.data_ingestion_config.to_date,
            data_file_path=file_path
            )
            logger.info(f"Metadata has been written.")
        except Exception as e:
            raise FinanceException(e,sys)

    def initiate_data_ingestion(self)-> DataIngestionArtifact:
        try:
            logger.info(f"Started downloading json file")
            self.download_monthly_files()

            if os.path.exists(self.data_ingestion_config.download_dir):

                logger.info(f"Converting and combining downloaded json into csv file")
                file_path = self.convert_files_to_parquet()
                self.write_metadata(file_path=file_path)
            return DataIngestionArtifact(data_file_path=os.path.join(self.data_ingestion_config.data_dir,self.data_ingestion_config.file_name)
            
            )
        except Exception as e:
            raise FinanceException(e, sys)



def main():
    try:
        config = FinanceConfig()
        data_ingestion_config = config.get_data_ingestion_config()
        data_ingestion = DataIngestion(data_ingestion_config=data_ingestion_config, n_month_interval=6)
        data_ingestion.initiate_data_ingestion()
    except Exception as e:
        raise FinanceException(e, sys)


if __name__ == "__main__":
    try:
        main()

    except Exception as e:
        logger.exception(e)
