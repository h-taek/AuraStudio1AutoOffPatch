/* macOS USB configuration helper. Default reads descriptors only.
 * --activate selects the declared configuration containing HID interface(s).
 * No firmware transfers, erase, reset, vendor requests, or PS writes. */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/usb/IOUSBLib.h>
#include <libkern/OSByteOrder.h>
#include <stdio.h>
#include <string.h>
static int number(io_service_t s,CFStringRef key){
 CFTypeRef p=IORegistryEntryCreateCFProperty(s,key,kCFAllocatorDefault,0);int v=-1;
 if(p&&CFGetTypeID(p)==CFNumberGetTypeID())CFNumberGetValue(p,kCFNumberIntType,&v);
 if(p)CFRelease(p);return v;
}
int main(int argc,char **argv){
 int activate=argc==2&&!strcmp(argv[1],"--activate");
 if(argc>2||(argc==2&&!activate))return 2;
 io_iterator_t it=0;io_service_t s=0,target=0;unsigned matches=0;
 kern_return_t kr=IOServiceGetMatchingServices(kIOMainPortDefault,IOServiceMatching("IOUSBHostDevice"),&it);
 if(kr){fprintf(stderr,"enumeration 0x%x\n",kr);return 1;}
 while((s=IOIteratorNext(it))){
  if(number(s,CFSTR("idVendor"))==0x0ecb&&number(s,CFSTR("idProduct"))==0x1eb2){matches++;if(!target)target=s;else IOObjectRelease(s);}else IOObjectRelease(s);
 }
 IOObjectRelease(it);if(matches!=1){fprintf(stderr,"Expected one target, found %u\n",matches);if(target)IOObjectRelease(target);return 1;}
 IOCFPlugInInterface **plugin=NULL;IOUSBDeviceInterface **d=NULL;SInt32 score=0;
 kr=IOCreatePlugInInterfaceForService(target,kIOUSBDeviceUserClientTypeID,kIOCFPlugInInterfaceID,&plugin,&score);IOObjectRelease(target);
 if(kr||!plugin){fprintf(stderr,"plugin 0x%x\n",kr);return 1;}
 HRESULT hr=(*plugin)->QueryInterface(plugin,CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID),(LPVOID*)&d);IODestroyPlugInInterface(plugin);
 if(hr||!d){fprintf(stderr,"interface 0x%x\n",(unsigned)hr);return 1;}
 UInt16 vid=0,pid=0;(*d)->GetDeviceVendor(d,&vid);(*d)->GetDeviceProduct(d,&pid);
 if(vid!=0x0ecb||pid!=0x1eb2){(*d)->Release(d);return 1;}
 kr=(*d)->USBDeviceOpen(d);if(kr){fprintf(stderr,"open 0x%x\n",kr);(*d)->Release(d);return 1;}
 UInt8 current=255,count=0;(*d)->GetConfiguration(d,&current);(*d)->GetNumberOfConfigurations(d,&count);
 printf("VID=%04x PID=%04x current_configuration=%u configurations=%u\n",vid,pid,current,count);
 int candidate=-1,hid_interfaces=0;
 for(UInt8 i=0;i<count;i++){
  IOUSBConfigurationDescriptorPtr c=NULL;kr=(*d)->GetConfigurationDescriptorPtr(d,i,&c);if(kr||!c)continue;
  UInt16 total=OSSwapLittleToHostInt16(c->wTotalLength);const unsigned char *p=(const unsigned char*)c;
  printf("configuration=%u descriptor_bytes=%u raw=",c->bConfigurationValue,total);for(unsigned j=0;j<total;j++)printf("%02x",p[j]);puts("");
  int hid=0;for(unsigned j=0;j+2<=total;){unsigned len=p[j],type=p[j+1];if(len<2||j+len>total)break;
   if(type==4&&len>=9){printf("interface=%u alternate=%u class=%u subclass=%u protocol=%u\n",p[j+2],p[j+3],p[j+5],p[j+6],p[j+7]);if(p[j+5]==3)hid++;}j+=len;
  }
  if(hid){candidate=c->bConfigurationValue;hid_interfaces+=hid;}
 }
 int result=0;
 if(activate){
  if(count!=1||candidate!=1||hid_interfaces!=1){fprintf(stderr,"Unexpected configuration profile; no activation\n");result=1;}
  else if(current==1)puts("Configuration already active; no change");
  else if(current!=0){fprintf(stderr,"Unexpected active configuration; no change\n");result=1;}
  else{kr=(*d)->SetConfiguration(d,1);printf("SetConfiguration(1) result=0x%x\n",kr);if(kr)result=1;}
 }
 (*d)->USBDeviceClose(d);(*d)->Release(d);return result;
}
